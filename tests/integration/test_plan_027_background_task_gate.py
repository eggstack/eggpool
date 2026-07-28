"""Plan 027 Workstream H — Background writer admission gate.

Verifies that background writers (dispatch, routing trace, metrics)
pause during database recovery and resume after the connection is
restored.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from pathlib import Path

from eggpool.db.connection import Database, DatabaseLifecycleState
from eggpool.db.migrations import MigrationRunner
from eggpool.db.recovery import DatabaseRecoveryController
from eggpool.db.repositories import RoutingDecisionRepository
from eggpool.db.rollup_repository import UsageRollupRepository
from eggpool.errors import DatabaseConnectionInvalidatedError
from eggpool.metrics.buffer import (
    MetricsWriteCoalescer,
    UsageMetricEvent,
)
from eggpool.models.config import DatabaseRecoveryConfig, MetricsConfig
from eggpool.observability.routing_trace_writer import (
    RoutingTraceEvent,
    RoutingTraceWriter,
)
from eggpool.request.dispatch_intent import (
    DispatchIntent,
    PersistedDispatchResult,
)
from eggpool.request.dispatch_writer import (
    DispatchPersistenceWriter,
    _QueuedIntent,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture()
async def test_db(tmp_path: Path) -> Database:
    """Provide a file-backed database with migrations applied."""
    db_path = str(tmp_path / "gate_test.db")
    db = Database(path=db_path)
    await db.connect()
    await MigrationRunner(db).run()
    return db


@pytest_asyncio.fixture()
async def recovery_config() -> DatabaseRecoveryConfig:
    return DatabaseRecoveryConfig(
        enabled=True,
        max_attempts=3,
        initial_backoff_ms=10,
        max_backoff_ms=100,
    )


@pytest_asyncio.fixture()
def metrics_config() -> MetricsConfig:
    return MetricsConfig(
        write_mode="balanced",
        flush_interval_s=30,
        max_buffered_events=500,
        timeseries_bucket_s=60,
    )


def _make_intent(proxy_request_id: str = "test-req-1") -> DispatchIntent:
    """Create a minimal DispatchIntent for testing."""
    return DispatchIntent(
        proxy_request_id=proxy_request_id,
        attempt_number=1,
        account_id=1,
        account_name="test-account",
        provider_id="test-provider",
        model_id="test-model",
        protocol="openai",
        streamed=False,
        estimated_tokens=100,
        estimated_microdollars=10,
        started_at=datetime.now(tz=UTC).isoformat(),
    )


def _make_trace_event() -> RoutingTraceEvent:
    """Create a minimal RoutingTraceEvent for testing."""
    return RoutingTraceEvent(
        request_id="test-req-1",
        db_request_id=1,
        attempt_number=1,
        model_id="test-model",
        provider_id="test-provider",
        protocol="openai",
        selected_account_name="test-account",
        selected_account_id=1,
        selected_tier=0,
        selected_score=0.5,
        eligible_count=2,
        scored_count=2,
        attempted_excluded_count=0,
        top_score=0.5,
        top_score_account_name="test-account",
        exclude_reasons_json="{}",
        score_components_json=None,
        created_at_mono_ns=0,
        created_at_epoch=0.0,
        generation_id=1,
    )


def _make_usage_event() -> UsageMetricEvent:
    """Create a minimal UsageMetricEvent for testing."""
    return UsageMetricEvent(
        timestamp=datetime.now(tz=UTC),
        provider_id="test-provider",
        model_id="test-model",
        account_id=1,
        protocol="openai",
        streamed=False,
        status="success",
        retry_count=0,
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
        thinking_characters=0,
        cost_microdollars=10,
        bytes_received=1024,
        bytes_emitted=512,
        latency_ms=100,
        first_byte_ms=50,
    )


@pytest.mark.integration
async def test_dispatch_writer_waits_for_admission(
    test_db: Database,
    recovery_config: DatabaseRecoveryConfig,
) -> None:
    """Dispatch writer pauses during recovery, resumes after."""
    controller = DatabaseRecoveryController(db=test_db, config=recovery_config)
    writer = DispatchPersistenceWriter(
        test_db,
        max_queue_depth=16,
        max_batch_size=1,
        max_batch_wait_ms=10.0,
    )
    try:
        writer.start()
        await asyncio.sleep(0.01)

        intent = _make_intent("gate-dispatch-1")
        future: Future[PersistedDispatchResult] = Future()
        qi = _QueuedIntent(intent=intent, future=future)
        writer._queue.put_nowait(qi)

        await test_db._invalidate_connection(reason="test invalidation")
        assert test_db.writes_admitted is False
        assert test_db.lifecycle_state is DatabaseLifecycleState.INVALIDATED

        await controller.handle_invalidation(
            reason="test invalidation",
            reason_class="other",
        )
        ready = await controller.wait_for_ready(timeout_s=5.0)
        assert ready is True

        assert test_db.writes_admitted is True

        result = await asyncio.wait_for(asyncio.wrap_future(future), timeout=5.0)
        assert result.batch_size == 1
    finally:
        await writer.stop()
        await controller.shutdown()


@pytest.mark.integration
async def test_routing_trace_writer_waits_for_admission(
    test_db: Database,
    recovery_config: DatabaseRecoveryConfig,
) -> None:
    """Routing trace writer pauses during recovery, resumes after."""
    controller = DatabaseRecoveryController(db=test_db, config=recovery_config)
    repo = RoutingDecisionRepository(test_db)
    writer = RoutingTraceWriter(
        test_db,
        repo,
        queue_capacity=100,
        flush_interval_s=0.1,
        max_batch_size=10,
    )
    try:
        writer.start()
        await asyncio.sleep(0.01)

        event = _make_trace_event()
        status = writer.submit(event)
        assert status == "accepted"

        await test_db._invalidate_connection(reason="test invalidation")
        assert test_db.writes_admitted is False

        await controller.handle_invalidation(
            reason="test invalidation",
            reason_class="other",
        )
        ready = await controller.wait_for_ready(timeout_s=5.0)
        assert ready is True

        assert test_db.writes_admitted is True

        snap = writer.snapshot()
        assert snap["alive"] is True
        assert snap["written"] >= 0
    finally:
        await writer.stop()
        await controller.shutdown()


@pytest.mark.integration
async def test_metrics_coalescer_waits_for_admission(
    test_db: Database,
    recovery_config: DatabaseRecoveryConfig,
    metrics_config: MetricsConfig,
) -> None:
    """Metrics coalescer pauses during recovery, resumes after."""
    repo = UsageRollupRepository(test_db)
    coalescer = MetricsWriteCoalescer(
        config=metrics_config,
        db=test_db,
        rollup_repo=repo,
    )
    try:
        event = _make_usage_event()
        coalescer.record_usage(event)

        test_db._recovery_controller = None
        await test_db._invalidate_connection(reason="test invalidation")
        assert test_db.writes_admitted is False

        snap = coalescer.snapshot()
        assert snap["buffered_events"] >= 1

        result = await coalescer.flush(reason="test_before_recovery")
        assert result.error_class == "WritesNotAdmitted"

        await test_db.connect()
        assert test_db.writes_admitted is True

        result = await coalescer.flush(reason="test_after_recovery")
        assert result.error_class is None
        assert result.rows_flushed >= 1
    finally:
        await test_db.disconnect()


@pytest.mark.integration
async def test_cleanup_tasks_handle_invalidated_connection(
    test_db: Database,
    recovery_config: DatabaseRecoveryConfig,
) -> None:
    """Cleanup functions handle invalidated connection gracefully."""
    controller = DatabaseRecoveryController(db=test_db, config=recovery_config)
    try:
        async with test_db.transaction():
            await test_db.execute_write(
                "CREATE TABLE IF NOT EXISTS cleanup_test "
                "(id INTEGER PRIMARY KEY, v TEXT)"
            )
            await test_db.execute_write(
                "INSERT INTO cleanup_test (v) VALUES (?)", ("row1",)
            )

        row = await test_db.fetch_one("SELECT v FROM cleanup_test WHERE id = 1")
        assert row is not None
        assert row["v"] == "row1"

        await test_db._invalidate_connection(reason="test invalidation")
        assert test_db.writes_admitted is False

        with pytest.raises(DatabaseConnectionInvalidatedError):
            await test_db.fetch_one("SELECT 1")

        await controller.handle_invalidation(
            reason="test invalidation",
            reason_class="other",
        )
        ready = await controller.wait_for_ready(timeout_s=5.0)
        assert ready is True

        assert test_db.writes_admitted is True

        row = await test_db.fetch_one("SELECT v FROM cleanup_test WHERE id = 1")
        assert row is not None
        assert row["v"] == "row1"
    finally:
        await controller.shutdown()
