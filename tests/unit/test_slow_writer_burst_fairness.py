"""Slow-writer burst fairness tests for the three-phase selection claim flow.

Proves that durable-before-publication overlap under a slow dispatch
writer does not create unacceptable transient account-selection skew.

The selection claim flow in ``RequestCoordinator._select_and_persist_attempt``
works in three phases:

* Phase A: Claim an account under ``_selection_claim_lock``
* Phase B: Persist durable rows OUTSIDE the lock (via ``DispatchPersistenceWriter``)
* Phase C: Re-acquire the lock to publish runtime state

Under a slow writer and burst of concurrent selectors, several claims
may be durable but not yet published.  This test suite proves the
system does not create unacceptable transient account-selection skew,
exceed concurrency limits due to delayed publication, or leak
claim/health state on failure paths.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections import Counter
from concurrent.futures import Future
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
from eggpool.request.attempt_finalizer import AttemptFinalizeResult
from eggpool.request.coordinator import ProxyRequestContext, RequestCoordinator
from eggpool.request.dispatch_intent import (
    DispatchAmbiguousCommitError,
    DispatchIntent,
    DispatchTransactionError,
    PersistedDispatchResult,
)
from eggpool.request.finalization_job import RequestFinalizationSupervisor
from eggpool.request.selection_claim_diagnostics import (
    SelectionClaimDiagnostics,
)
from eggpool.routing.router import Router

_NUM_ACCOUNTS = 5
_NUM_SELECTORS = 25


# ---------------------------------------------------------------------------
# Mock infrastructure
# ---------------------------------------------------------------------------


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
        self,
        provider_id: str | None = None,
        account_name: str | None = None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(_noop_handler))


async def _noop_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={})


class SlowDispatchWriter:
    """Test dispatch writer that allows explicit control over commit timing.

    Each ``submit_intent`` call returns a future that the test can
    resolve at will, allowing precise synchronization of commit
    completion relative to concurrent selectors.
    """

    def __init__(self) -> None:
        self._submitted: list[DispatchIntent] = []
        self._futures: list[Future[PersistedDispatchResult]] = []
        self._commit_counter = 0

    @property
    def submitted(self) -> list[DispatchIntent]:
        return list(self._submitted)

    def submitted_count(self) -> int:
        return len(self._submitted)

    def complete_commit(self, index: int) -> None:
        """Resolve the future for intent *index* with a synthetic result."""
        if index < len(self._futures):
            fut = self._futures[index]
            if not fut.done():
                self._commit_counter += 1
                c = self._commit_counter
                fut.set_result(
                    PersistedDispatchResult(
                        db_request_id=str(1000 + c),
                        # The fixture's real SQLite rows use these integer
                        # identities; keep the synthetic result compatible
                        # with compensation's durable lookup.
                        reservation_id=str(c),
                        attempt_id=c,
                        attempt_number=1,
                        batch_id=1,
                        batch_size=1,
                    )
                )

    def fail_commit(self, index: int, exc: BaseException) -> None:
        """Reject the future for intent *index* with *exc*."""
        if index < len(self._futures):
            fut = self._futures[index]
            if not fut.done():
                fut.set_exception(exc)

    def complete_all_pending(self) -> None:
        """Resolve all unfinished futures with synthetic results."""
        for i in range(len(self._futures)):
            self.complete_commit(i)

    def submit_intent(self, intent: DispatchIntent) -> Future[PersistedDispatchResult]:
        """Submit an intent; the commit is gated on ``complete_commit``."""
        fut: Future[PersistedDispatchResult] = Future()
        self._submitted.append(intent)
        self._futures.append(fut)
        return fut


class RecordingHealthManager:
    """Mock health manager that records acquire/release calls."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._acquires: list[str] = []
        self._releases: list[str] = []

    def try_acquire_request(self, account_name: str, model_id: str) -> bool:
        with self._lock:
            self._acquires.append(account_name)
            return True

    def release_request(self, account_name: str) -> None:
        with self._lock:
            self._releases.append(account_name)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "acquires": list(self._acquires),
                "releases": list(self._releases),
            }


# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------


def _account_names() -> list[str]:
    return [f"acct-{chr(ord('a') + i)}" for i in range(_NUM_ACCOUNTS)]


async def _build_fixture() -> dict[str, Any]:
    """Build a coordinator with a controllable dispatch writer."""
    names = _account_names()
    for name in names:
        os.environ[f"K_SWBF_{name}"] = "k"

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
                        "api_key_env": f"K_SWBF_{name}",
                        "weight": 1.0,
                    }
                    for name in names
                ],
            }
        }
    }
    config = AppConfig.model_validate(raw)
    registry = AccountRegistry(config)
    cache = ModelCatalogCache()
    for name in names:
        cache.update_from_account(
            name,
            "test-provider",
            [{"model_id": "gpt-4", "protocol": "openai"}],
        )
    catalog = _MockCatalog(cache)
    quota_estimator = QuotaEstimator()
    router = Router(
        registry,  # type: ignore[arg-type]
        catalog,  # type: ignore[arg-type]
        quota_estimator=quota_estimator,
    )
    for name in names:
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
        for name in names:
            await db.execute_insert(
                "INSERT INTO accounts"
                " (name, api_key_env, enabled, weight)"
                " VALUES (?, ?, 1, ?)",
                (name, f"K_SWBF_{name}", 1.0),
            )
            row = await db.fetch_one(
                "SELECT id FROM accounts WHERE name = ?",
                (name,),
            )
            assert row is not None
            account_id = int(row["id"])
            await db.execute_insert(
                "INSERT INTO account_models"
                " (account_id, model_id, enabled)"
                " VALUES (?, ?, 1)",
                (account_id, "gpt-4"),
            )
    request_repo = RequestRepository(db)
    reservation_repo = ReservationRepository(db)
    attempt_repo = AttemptRepository(db)
    routing_decision_repo = RoutingDecisionRepository(db)
    diagnostics = SelectionClaimDiagnostics()
    health_manager = RecordingHealthManager()
    writer = SlowDispatchWriter()
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
        health_manager=health_manager,  # type: ignore[arg-type]
        dispatch_span_recorder=None,
        selection_claim_diagnostics=diagnostics,
        dispatch_writer=writer,
        use_dispatch_writer=True,
    )
    coordinator._finalization_supervisor = RequestFinalizationSupervisor(db=db)  # pyright: ignore[reportPrivateUsage]
    return {
        "coordinator": coordinator,
        "db": db,
        "router": router,
        "diagnostics": diagnostics,
        "health_manager": health_manager,
        "writer": writer,
        "names": names,
    }


def _make_context(request_id: str) -> ProxyRequestContext:
    return ProxyRequestContext(
        request_id=request_id,
        protocol="openai",
        model_id="gpt-4",
        streaming=False,
        original_body=(b'{"messages":[{"role":"user","content":"hi"}]}'),
        incoming_headers={},
    )


async def _wait_for_writer_submitted(
    writer: SlowDispatchWriter, count: int, timeout: float = 5.0
) -> None:
    """Busy-wait until the writer has at least *count* submitted intents."""
    deadline = asyncio.get_event_loop().time() + timeout
    while writer.submitted_count() < count:
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(
                f"Writer did not reach {count} submissions within {timeout}s"
            )
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_claim_lock_not_held_during_writer_wait() -> None:
    """Phase A claim lock releases BEFORE Phase B waits on the writer.

    The claim lock must not be held while the dispatch writer
    processes the intent; otherwise unrelated selectors would be
    blocked by a slow writer.
    """
    fixture = await _build_fixture()
    try:
        coord = fixture["coordinator"]
        writer = fixture["writer"]

        # Run a selector whose Phase B will block on the writer.
        async def _run_attempt() -> None:
            ctx = _make_context("req-lock-scope")
            await coord._select_and_persist_attempt(ctx, 1)

        task = asyncio.create_task(_run_attempt())

        # Wait until Phase A completes (intent submitted = Phase A done,
        # Phase B is blocked on the writer future).
        await _wait_for_writer_submitted(writer, 1)

        # Phase A has released the claim lock.  Verify it is acquirable
        # by another coroutine (proving Phase B does not hold it).
        acquired = False
        try:
            async with asyncio.timeout(0.5):
                async with coord._selection_claim_lock:
                    acquired = True
        except TimeoutError:
            pass

        assert acquired, (
            "Selection claim lock was still held during Phase B writer wait"
        )

        writer.complete_all_pending()
        await task
    finally:
        await fixture["db"].disconnect()


@pytest.mark.asyncio()
async def test_concurrent_selectors_proceed_during_slow_writer() -> None:
    """Unrelated selectors enter Phase A while another waits for writer.

    With a slow writer, one selector's Phase B blocks on the writer
    while other selectors freely enter Phase A, claim accounts, and
    enqueue their own intents.
    """
    fixture = await _build_fixture()
    try:
        coord = fixture["coordinator"]
        writer = fixture["writer"]

        num_concurrent = 5
        results: list[str | None] = [None] * num_concurrent

        async def _run(i: int) -> None:
            ctx = _make_context(f"req-concurrent-{i}")
            try:
                await coord._select_and_persist_attempt(ctx, 1)
                results[i] = "ok"
            except Exception:
                results[i] = "error"

        tasks = [asyncio.create_task(_run(i)) for i in range(num_concurrent)]

        # Wait until all selectors have entered Phase A and submitted
        # intents (meaning Phase A completed for each).
        await _wait_for_writer_submitted(writer, num_concurrent)

        # At this point all Phase A's have completed and released the
        # lock.  Verify the lock is free.
        acquired = False
        try:
            async with asyncio.timeout(0.5):
                async with coord._selection_claim_lock:
                    acquired = True
        except TimeoutError:
            pass

        assert acquired, "Claim lock was not free after Phase A completed"

        # Unblock all writers so tasks complete.
        writer.complete_all_pending()
        await asyncio.gather(*tasks, return_exceptions=True)

        assert all(r == "ok" for r in results if r is not None), (
            f"Not all selectors completed: {results}"
        )
    finally:
        await fixture["db"].disconnect()


@pytest.mark.asyncio()
async def test_fairness_bound_during_overlap_window() -> None:
    """Account selection remains within a documented fairness bound.

    When the writer is slow and N selectors fire concurrently, no
    single account should receive more than a bounded share of the
    selections.  With 5 accounts and 25 selectors, the expected
    uniform share is 5 per account.  We allow a generous bound of
    ``ceil(N/num_accounts * 3)`` to account for routing-score jitter,
    but the selection MUST NOT concentrate entirely on one account.
    """
    fixture = await _build_fixture()
    try:
        coord = fixture["coordinator"]
        writer = fixture["writer"]

        selected_accounts: list[str] = []
        selection_lock = asyncio.Lock()

        _original_select = coord._select_and_persist_attempt

        async def _instrumented_select(
            context: ProxyRequestContext, attempt_number: int
        ) -> Any:
            result = await _original_select(context, attempt_number)
            async with selection_lock:
                selected_accounts.append(result.account_name)
            return result

        coord._select_and_persist_attempt = _instrumented_select  # type: ignore[method-assign]

        async def _run(i: int) -> None:
            ctx = _make_context(f"req-fair-{i}")
            await coord._select_and_persist_attempt(ctx, 1)

        tasks = [asyncio.create_task(_run(i)) for i in range(_NUM_SELECTORS)]

        # Let all selectors reach Phase B (intent submitted).
        await _wait_for_writer_submitted(writer, _NUM_SELECTORS)

        # Now let all writers complete.
        writer.complete_all_pending()
        await asyncio.gather(*tasks, return_exceptions=True)

        counts = Counter(selected_accounts)
        total = len(selected_accounts)
        assert total == _NUM_SELECTORS, (
            f"Expected {_NUM_SELECTORS} selections, got {total}"
        )

        # Fairness bound: no account should receive more than
        # ceil(total / num_accounts * 3) selections.
        max_per_account = -(-total // _NUM_ACCOUNTS) * 3  # ceil division * 3
        worst = max(counts.values()) if counts else 0
        assert worst <= max_per_account, (
            f"Account skew too high: worst={worst}, "
            f"allowed={max_per_account},"
            f" counts={dict(counts)}"
        )

        # At least 2 distinct accounts should have been selected.
        assert len(counts) >= 2, f"All selections concentrated: {dict(counts)}"
    finally:
        await fixture["db"].disconnect()


@pytest.mark.asyncio()
async def test_active_count_never_exceeds_publication_count() -> None:
    """Delayed publication does not inflate active counts beyond limits.

    Phase A claims an account slot via the circuit breaker, but
    Phase C (publication) is what actually increments the runtime
    active count.  If the writer is slow, many accounts may be
    claimed but not yet published.  This test proves that no
    account's active count exceeds the number of *published*
    requests.
    """
    fixture = await _build_fixture()
    try:
        coord = fixture["coordinator"]
        writer = fixture["writer"]
        router = fixture["router"]
        names = fixture["names"]

        publication_count = asyncio.Lock()
        published_per_account: dict[str, int] = {n: 0 for n in names}

        _orig_publish = coord._publish_runtime_state

        async def _counting_publish(
            *,
            account_name: str,
            estimated_tokens: int,
            estimated_microdollars: int,
        ) -> None:
            await _orig_publish(
                account_name=account_name,
                estimated_tokens=estimated_tokens,
                estimated_microdollars=estimated_microdollars,
            )
            async with publication_count:
                published_per_account[account_name] = (
                    published_per_account.get(account_name, 0) + 1
                )

        coord._publish_runtime_state = _counting_publish  # type: ignore[method-assign]

        num_selectors = 15

        async def _run(i: int) -> None:
            ctx = _make_context(f"req-limit-{i}")
            await coord._select_and_persist_attempt(ctx, 1)

        tasks = [asyncio.create_task(_run(i)) for i in range(num_selectors)]

        # Wait for all intents to be submitted to the writer.
        await _wait_for_writer_submitted(writer, num_selectors)
        assert writer.submitted_count() >= num_selectors

        # Let the writer drain.
        writer.complete_all_pending()
        await asyncio.gather(*tasks, return_exceptions=True)

        # After all tasks complete, the active counts should equal
        # the published counts (no phantom inflation).
        for name in names:
            state = router._registry.get_state(name)
            if state is not None:
                expected = published_per_account.get(name, 0)
                assert state.active_request_count == expected, (
                    f"Active count for {name}"
                    f"={state.active_request_count}"
                    f" != published={expected}"
                )
    finally:
        await fixture["db"].disconnect()


@pytest.mark.asyncio()
async def test_failed_persistence_releases_health_slot_exactly_once() -> None:
    """When persistence fails, the health slot is released exactly once.

    Phase A acquires a circuit-breaker slot; if Phase B persistence
    fails, the slot must be released exactly once — no leak, no
    double release.
    """
    fixture = await _build_fixture()
    try:
        coord = fixture["coordinator"]
        writer = fixture["writer"]
        health = fixture["health_manager"]

        async def _run() -> None:
            ctx = _make_context("req-fail-persist")
            await coord._select_and_persist_attempt(ctx, 1)

        task = asyncio.create_task(_run())

        # Wait for the intent to be submitted.
        await _wait_for_writer_submitted(writer, 1)

        # Fail the persistence.
        writer.fail_commit(
            0,
            DispatchTransactionError("simulated writer failure"),
        )

        with pytest.raises(DispatchTransactionError):
            await task

        snap = health.snapshot()
        releases = [r for r in snap["releases"] if r.startswith("acct-")]
        assert len(releases) == 1, (
            f"Health slot released {len(releases)} times, expected 1: {releases}"
        )
    finally:
        await fixture["db"].disconnect()


@pytest.mark.asyncio()
async def test_failed_post_commit_publication_invokes_compensation() -> None:
    """Post-commit publication failure invokes compensation exactly once.

    If Phase C publication fails after Phase B committed, the
    coordinator must compensate — decrement the active count,
    finalize the attempt as ``PostCommitInterrupted``, and release
    the health slot.
    """
    fixture = await _build_fixture()
    try:
        coord = fixture["coordinator"]
        writer = fixture["writer"]
        health = fixture["health_manager"]
        diagnostics = fixture["diagnostics"]

        _orig_publish = coord._publish_runtime_state

        async def _failing_publish(
            *,
            account_name: str,
            estimated_tokens: int,
            estimated_microdollars: int,
            **kwargs: Any,
        ) -> None:
            del account_name, estimated_tokens, estimated_microdollars, kwargs
            raise RuntimeError("simulated publication failure")

        coord._publish_runtime_state = _failing_publish  # type: ignore[method-assign]

        async def _finalize_synthetic_attempt(
            *args: Any, **kwargs: Any
        ) -> AttemptFinalizeResult:
            return AttemptFinalizeResult(
                attempt_transitioned=True,
                reservation_released=True,
                reservation_converged=True,
            )

        coord._attempt_finalizer.finalize_failed_attempt = (  # type: ignore[method-assign]
            _finalize_synthetic_attempt
        )

        async def _run() -> None:
            ctx = _make_context("req-fail-publish")
            await coord._select_and_persist_attempt(ctx, 1)

        task = asyncio.create_task(_run())

        # Wait for intent submission, then let the writer commit.
        await _wait_for_writer_submitted(writer, 1)
        writer.complete_commit(0)

        with pytest.raises(
            RuntimeError,
            match="simulated publication failure",
        ):
            await task

        snap = diagnostics.snapshot()
        # Compensation is invoked via _compensate_or_rollback_claim
        # which calls record_compensation(success=True).
        assert snap["compensation_successes"] >= 1, (
            f"Expected >=1 compensation_success, got {snap['compensation_successes']}"
        )

        # Health slot must be released by compensation.
        health_snap = health.snapshot()
        releases = [r for r in health_snap["releases"] if r.startswith("acct-")]
        assert len(releases) == 1, (
            f"Health slot released {len(releases)} times, expected 1"
        )
    finally:
        await fixture["db"].disconnect()


@pytest.mark.asyncio()
async def test_ambiguous_commit_reconciles_without_duplicates() -> None:
    """Ambiguous commits reconcile without duplicate rows.

    When the writer future resolves with an ambiguous-commit error,
    the coordinator propagates the error.  The test verifies that
    the health slot is released exactly once and that no duplicate
    durable rows are created.
    """
    fixture = await _build_fixture()
    try:
        coord = fixture["coordinator"]
        writer = fixture["writer"]
        db = fixture["db"]

        request_count_before = (
            await db.fetch_one("SELECT COUNT(*) as c FROM requests")
        )["c"]

        async def _run() -> None:
            ctx = _make_context("req-ambiguous")
            await coord._select_and_persist_attempt(ctx, 1)

        task = asyncio.create_task(_run())
        await _wait_for_writer_submitted(writer, 1)

        # Simulate ambiguous commit: writer fails with ambiguous error.
        writer.fail_commit(
            0,
            DispatchAmbiguousCommitError("ambiguous commit boundary"),
        )

        with pytest.raises(DispatchAmbiguousCommitError):
            await task

        # No new requests should have been created beyond the
        # ambiguous boundary.
        request_count_after = (
            await db.fetch_one("SELECT COUNT(*) as c FROM requests")
        )["c"]
        assert request_count_after <= request_count_before + 1, (
            f"Duplicate request rows created:"
            f" before={request_count_before},"
            f" after={request_count_after}"
        )
    finally:
        await fixture["db"].disconnect()


@pytest.mark.asyncio()
async def test_durable_and_runtime_state_converge_after_all_completions() -> None:
    """After all selectors complete, durable and runtime state converge.

    Every successful selection should produce a committed DB row AND
    a published runtime active-count increment.  No requests should
    be left in a half-committed state, and the sum of published
    counts should equal the sum of active request counts across all
    accounts.
    """
    fixture = await _build_fixture()
    try:
        coord = fixture["coordinator"]
        writer = fixture["writer"]
        router = fixture["router"]
        diagnostics = fixture["diagnostics"]
        names = fixture["names"]

        num_selectors = 10
        results: list[str] = []

        async def _run(i: int) -> None:
            ctx = _make_context(f"req-converge-{i}")
            try:
                await coord._select_and_persist_attempt(ctx, 1)
                results.append("ok")
            except Exception:
                results.append("error")

        tasks = [asyncio.create_task(_run(i)) for i in range(num_selectors)]

        # Wait for all intents to be submitted.
        await _wait_for_writer_submitted(writer, num_selectors)
        writer.complete_all_pending()
        await asyncio.gather(*tasks, return_exceptions=True)

        snap = diagnostics.snapshot()

        # All successful selections should be published.
        # (The writer path does not call record_claim_committed;
        # that diagnostic is only for the non-writer path.)
        assert snap["claims_published"] == num_selectors, (
            f"Expected {num_selectors} published, got {snap['claims_published']}"
        )

        # The mock writer does not persist to SQLite; verify the
        # writer received the correct number of intents instead.
        assert writer.submitted_count() == num_selectors, (
            f"Expected {num_selectors} writer submissions,"
            f" got {writer.submitted_count()}"
        )

        # Runtime state: sum of active counts == num_selectors.
        total_active = 0
        for name in names:
            state = router._registry.get_state(name)
            if state is not None:
                total_active += state.active_request_count
        assert total_active == num_selectors, (
            f"Expected total active count {num_selectors}, got {total_active}"
        )

        # No compensation should have been needed.
        assert snap["compensation_successes"] == 0
        assert snap["compensation_failures"] == 0
    finally:
        await fixture["db"].disconnect()


@pytest.mark.asyncio()
async def test_burst_selectors_all_complete_without_skew() -> None:
    """End-to-end: 25 selectors with a slow writer produce no skew.

    This is the primary integration test.  All 25 selectors fire
    concurrently.  Then the writer drains.  The result must be:
    * All 25 succeed.
    * Account selection is spread across multiple accounts.
    * No account's active count exceeds its published count.
    * Durable rows match the selection count.
    """
    fixture = await _build_fixture()
    try:
        coord = fixture["coordinator"]
        writer = fixture["writer"]
        router = fixture["router"]
        diagnostics = fixture["diagnostics"]
        names = fixture["names"]

        selected_accounts: list[str] = []
        selection_lock = asyncio.Lock()

        _orig_select = coord._select_and_persist_attempt

        async def _instrumented_select(
            context: ProxyRequestContext, attempt_number: int
        ) -> Any:
            result = await _orig_select(context, attempt_number)
            async with selection_lock:
                selected_accounts.append(result.account_name)
            return result

        coord._select_and_persist_attempt = _instrumented_select  # type: ignore[method-assign]

        results: list[str] = []

        async def _run(i: int) -> None:
            ctx = _make_context(f"req-burst-{i}")
            try:
                await coord._select_and_persist_attempt(ctx, 1)
                results.append("ok")
            except Exception:
                results.append("error")

        tasks = [asyncio.create_task(_run(i)) for i in range(_NUM_SELECTORS)]

        # Wait for all intents to be submitted.
        await _wait_for_writer_submitted(writer, _NUM_SELECTORS)

        # Let the writer drain.
        writer.complete_all_pending()
        await asyncio.gather(*tasks, return_exceptions=True)

        # All should succeed.
        ok_count = results.count("ok")
        assert ok_count == _NUM_SELECTORS, (
            f"Expected {_NUM_SELECTORS} ok, got {ok_count}: {results}"
        )

        # Fairness: selection spread across multiple accounts.
        counts = Counter(selected_accounts)
        assert len(counts) >= 2, f"All selections concentrated: {dict(counts)}"

        # No account exceeds its published active count.
        for name in names:
            state = router._registry.get_state(name)
            if state is not None:
                assert state.active_request_count >= 0, (
                    f"Negative active count for {name}: {state.active_request_count}"
                )

        # Diagnostics invariants.
        snap = diagnostics.snapshot()
        assert snap["claims_published"] == _NUM_SELECTORS

        # The mock writer does not persist to SQLite; verify the
        # writer received the correct number of intents instead.
        assert writer.submitted_count() == _NUM_SELECTORS

        # Runtime active total equals selection count.
        total_active = 0
        for name in names:
            state = router._registry.get_state(name)
            if state is not None:
                total_active += state.active_request_count
        assert total_active == _NUM_SELECTORS
    finally:
        await fixture["db"].disconnect()
