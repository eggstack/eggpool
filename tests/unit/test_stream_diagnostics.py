"""Unit tests for ``eggpool.request.stream_diagnostics``.

Covers the process-local outcome counter service that backs the
runtime dashboard's stream-stability section.  Validates:

- counter increments for terminal outcomes;
- bounded ring histograms;
- stable empty snapshot contract before any operations;
- httpx / upstream exception class breakdown;
- integration with the coordinator stream generator.
"""

from __future__ import annotations

import asyncio
import collections
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from eggpool.request.stream_diagnostics import (
    STREAM_OUTCOME_CLIENT_CANCELLED,
    STREAM_OUTCOME_COMPLETED,
    STREAM_OUTCOME_FINALIZER_FAILED,
    STREAM_OUTCOME_FINALIZER_TIMEOUT,
    STREAM_OUTCOME_IDLE_TIMEOUT,
    STREAM_OUTCOME_UPSTREAM_CONNECT_ERROR,
    STREAM_OUTCOME_UPSTREAM_CONNECT_TIMEOUT,
    STREAM_OUTCOME_UPSTREAM_MIDSTREAM_ERROR,
    STREAM_OUTCOME_UPSTREAM_POOL_TIMEOUT,
    STREAM_OUTCOME_UPSTREAM_PROTOCOL_ERROR,
    STREAM_OUTCOME_UPSTREAM_READ_TIMEOUT,
    STREAM_OUTCOME_UPSTREAM_TRANSPORT_ERROR,
    STREAM_OUTCOME_UPSTREAM_WRITE_TIMEOUT,
    ProviderStreamTimeoutError,
    StreamDiagnostics,
    classify_httpx_error_class,
    get_stream_diagnostics,
    reset_stream_diagnostics_for_tests,
)


def _timeout_stream_coordinator() -> tuple[Any, Any, Any]:
    from eggpool.request.coordinator import RequestCoordinator, SelectedAttempt
    from eggpool.retry.classification import RetryClassifier

    coordinator = object.__new__(RequestCoordinator)
    coordinator._classifier = RetryClassifier()
    coordinator._transcoder_policy = None
    coordinator._persist_error_detail = False
    coordinator._account_backoff_repo = None
    coordinator._router = MagicMock()
    coordinator._health_manager = MagicMock()
    coordinator._quota_estimator = MagicMock()
    coordinator._stream_diagnostics = StreamDiagnostics()
    coordinator._finalizer = MagicMock()
    coordinator._finalizer.finalize = AsyncMock()
    coordinator._finalizer.apply_runtime_convergence = AsyncMock()
    coordinator._effects_applier = MagicMock()
    context = SimpleNamespace(
        request_id="timeout-request",
        model_id="model-a",
        protocol="openai",
        upstream_protocol="openai",
        original_body=b"{}",
        original_body_size=2,
        client_metadata={},
        transcode_context=None,
        thinking_trace=None,
        segmentation=None,
        segmentation_not_collected=False,
        compression_observation=None,
        compression_result=None,
        resolved_compression_policy=None,
        synthetic_cache_result=None,
        upstream_connect_ms=0,
        upstream_headers_ms=None,
        response_handoff=SimpleNamespace(started=False),
    )
    selected = SelectedAttempt(
        attempt_id=1,
        proxy_request_id="timeout-request",
        runtime_lease=None,
        db_request_id="db-timeout",
        reservation_id="reservation-timeout",
        account_id=1,
        provider_id="provider-a",
        account_name="account-a",
        api_key="key-a",
        model_id="model-a",
        estimated_tokens=0,
        estimated_microdollars=0,
        attempt_number=1,
    )

    class _TestSupervisor:
        def register_or_get(
            self,
            _identity: Any,
            _outcome: Any,
            *,
            finalization_data: Any,
            **_kwargs: Any,
        ) -> Any:
            async def run() -> None:
                await coordinator._finalizer.finalize(selected, finalization_data)

            return SimpleNamespace(
                set_dependencies=lambda **_dependencies: None,
                run=run,
            )

    coordinator._finalization_supervisor = _TestSupervisor()
    return coordinator, context, selected


def test_empty_snapshot_contract() -> None:
    """Empty snapshot is stable before any operations."""
    diag = StreamDiagnostics()
    snap = diag.snapshot()
    assert snap["outcomes"] == {
        STREAM_OUTCOME_COMPLETED: 0,
        STREAM_OUTCOME_CLIENT_CANCELLED: 0,
        "client_cancelled": 0,  # alias is canonical too
        STREAM_OUTCOME_UPSTREAM_MIDSTREAM_ERROR: 0,
        STREAM_OUTCOME_FINALIZER_TIMEOUT: 0,
        STREAM_OUTCOME_FINALIZER_FAILED: 0,
        STREAM_OUTCOME_UPSTREAM_POOL_TIMEOUT: 0,
        STREAM_OUTCOME_UPSTREAM_READ_TIMEOUT: 0,
        STREAM_OUTCOME_UPSTREAM_CONNECT_TIMEOUT: 0,
        STREAM_OUTCOME_UPSTREAM_WRITE_TIMEOUT: 0,
        STREAM_OUTCOME_UPSTREAM_PROTOCOL_ERROR: 0,
        STREAM_OUTCOME_UPSTREAM_CONNECT_ERROR: 0,
        STREAM_OUTCOME_UPSTREAM_TRANSPORT_ERROR: 0,
        "stream_completed_canonical": 0,
        "stream_completed_compatibility": 0,
        "empty_eof": 0,
        "premature_eof_before_body": 0,
        "premature_eof_midstream": 0,
        "malformed_eof": 0,
        "first_byte_timeout": 0,
        "stream_idle_timeout": 0,
        "stream_lifetime_timeout": 0,
        "response_header_timeout": 0,
    }
    assert snap["httpx_exception_counts"] == {}
    assert snap["upstream_error_class_counts"] == {}
    assert snap["last_event"] is None
    assert snap["last_event_age_ms"] is None
    assert snap["completed_ms"]["sample_count"] == 0
    assert snap["client_cancel_ms"]["sample_count"] == 0
    assert snap["finalizer_timeout_ms"]["sample_count"] == 0


def test_outcome_increments_and_histogram_records() -> None:
    diag = StreamDiagnostics()
    diag.record_outcome(
        STREAM_OUTCOME_COMPLETED,
        proxy_request_id="req-1",
        elapsed_ms=120,
        bytes_emitted=42,
    )
    diag.record_outcome(
        STREAM_OUTCOME_COMPLETED,
        proxy_request_id="req-2",
        elapsed_ms=240,
        bytes_emitted=84,
    )
    diag.record_outcome(
        STREAM_OUTCOME_CLIENT_CANCELLED,
        proxy_request_id="req-3",
        elapsed_ms=80,
    )
    diag.record_outcome(
        STREAM_OUTCOME_UPSTREAM_MIDSTREAM_ERROR,
        proxy_request_id="req-4",
        elapsed_ms=300,
        exception_class="RemoteProtocolError",
    )
    diag.record_outcome(
        STREAM_OUTCOME_FINALIZER_TIMEOUT,
        proxy_request_id="req-5",
        elapsed_ms=10000,
    )
    snap = diag.snapshot()
    assert snap["outcomes"][STREAM_OUTCOME_COMPLETED] == 2
    assert snap["outcomes"][STREAM_OUTCOME_CLIENT_CANCELLED] == 1
    assert snap["outcomes"][STREAM_OUTCOME_UPSTREAM_MIDSTREAM_ERROR] == 1
    assert snap["outcomes"][STREAM_OUTCOME_FINALIZER_TIMEOUT] == 1
    assert snap["upstream_error_class_counts"] == {"RemoteProtocolError": 1}
    assert snap["completed_ms"]["sample_count"] == 2
    assert snap["client_cancel_ms"]["sample_count"] == 1
    assert snap["finalizer_timeout_ms"]["sample_count"] == 1
    assert snap["completed_ms"]["p50_ms"] == 120.0
    assert snap["client_cancel_ms"]["max_ms"] == 80.0
    assert snap["finalizer_timeout_ms"]["max_ms"] == 10000.0
    last = snap["last_event"]
    assert last is not None
    assert last["outcome"] == STREAM_OUTCOME_FINALIZER_TIMEOUT
    assert last["proxy_request_id"] == "req-5"


def test_unknown_outcome_bucket() -> None:
    diag = StreamDiagnostics()
    diag.record_outcome("totally_unrecognized_outcome")
    snap = diag.snapshot()
    assert snap["outcomes"]["unknown"] == 1


def test_httpx_exception_counts_separated_from_upstream() -> None:
    diag = StreamDiagnostics()
    diag.record_outcome(
        STREAM_OUTCOME_FINALIZER_FAILED,
        exception_class="PoolTimeout",
    )
    diag.record_outcome(
        STREAM_OUTCOME_UPSTREAM_MIDSTREAM_ERROR,
        exception_class="ReadTimeout",
    )
    snap = diag.snapshot()
    assert snap["httpx_exception_counts"] == {"PoolTimeout": 1}
    assert snap["upstream_error_class_counts"] == {"ReadTimeout": 1}


def test_get_stream_diagnostics_is_singleton() -> None:
    a = get_stream_diagnostics()
    b = get_stream_diagnostics()
    assert a is b
    # The reset helper returns a fresh instance for tests.
    fresh = reset_stream_diagnostics_for_tests()
    assert fresh is not a
    assert get_stream_diagnostics() is fresh


def test_bounded_ring_does_not_grow_unbounded() -> None:
    diag = StreamDiagnostics(histogram_capacity=8)
    for i in range(100):
        diag.record_outcome(
            STREAM_OUTCOME_COMPLETED,
            elapsed_ms=i,
            bytes_emitted=i,
        )
    snap = diag.snapshot()
    assert snap["completed_ms"]["sample_count"] == 8
    assert snap["completed_ms"]["max_ms"] == 99.0


@pytest.mark.asyncio()
async def test_idle_timeout_is_distinct_and_closes_upstream_response() -> None:
    coordinator, context, selected = _timeout_stream_coordinator()

    async def chunks() -> Any:
        yield b"data: first\n\n"
        await asyncio.sleep(0.05)
        yield b"data: second\n\n"

    class Response:
        headers = {"content-type": "text/event-stream"}

        def aiter_bytes(self) -> Any:
            return chunks()

        async def aclose(self) -> None:
            self.closed = True

    response = Response()
    stream = coordinator._build_stream_generator(
        context=context,
        upstream_response=response,
        selected=selected,
        resp_headers=[],
        stream_idle_timeout_s=0.01,
    )
    assert await anext(stream) == b"data: first\n\n"
    with pytest.raises(ProviderStreamTimeoutError) as error:
        await anext(stream)
    assert error.value.outcome == STREAM_OUTCOME_IDLE_TIMEOUT
    assert response.closed is True
    assert (
        coordinator._stream_diagnostics.snapshot()["outcomes"][
            STREAM_OUTCOME_IDLE_TIMEOUT
        ]
        == 1
    )
    coordinator._finalizer.finalize.assert_awaited_once()


def test_new_httpx_outcome_labels_exist_in_default_counter_set() -> None:
    """All first-class HTTPX transport outcome labels are present with value 0."""
    diag = StreamDiagnostics()
    snap = diag.snapshot()
    for label in (
        STREAM_OUTCOME_UPSTREAM_POOL_TIMEOUT,
        STREAM_OUTCOME_UPSTREAM_READ_TIMEOUT,
        STREAM_OUTCOME_UPSTREAM_CONNECT_TIMEOUT,
        STREAM_OUTCOME_UPSTREAM_WRITE_TIMEOUT,
        STREAM_OUTCOME_UPSTREAM_PROTOCOL_ERROR,
        STREAM_OUTCOME_UPSTREAM_CONNECT_ERROR,
        STREAM_OUTCOME_UPSTREAM_TRANSPORT_ERROR,
    ):
        assert label in snap["outcomes"], f"{label} missing from outcomes"
        assert snap["outcomes"][label] == 0, f"{label} should start at 0"


def test_classify_httpx_error_class_known_mappings() -> None:
    """classify_httpx_error_class maps every known HTTPX class."""
    cases = [
        ("PoolTimeout", STREAM_OUTCOME_UPSTREAM_POOL_TIMEOUT),
        ("ReadTimeout", STREAM_OUTCOME_UPSTREAM_READ_TIMEOUT),
        ("ConnectTimeout", STREAM_OUTCOME_UPSTREAM_CONNECT_TIMEOUT),
        ("WriteTimeout", STREAM_OUTCOME_UPSTREAM_WRITE_TIMEOUT),
        (
            "RemoteProtocolError",
            STREAM_OUTCOME_UPSTREAM_PROTOCOL_ERROR,
        ),
        ("ConnectError", STREAM_OUTCOME_UPSTREAM_CONNECT_ERROR),
    ]
    for cls, expected in cases:
        assert classify_httpx_error_class(cls) == expected


def test_classify_httpx_error_class_unknown_maps_to_transport_error() -> None:
    """Unknown exception classes fall back to upstream_transport_error."""
    for cls in ("ReadError", "WriteError", "SomeFutureHttpxError"):
        assert classify_httpx_error_class(cls) == (
            STREAM_OUTCOME_UPSTREAM_TRANSPORT_ERROR
        )


def test_first_class_outcome_increments_on_record() -> None:
    """Recording a first-class HTTPX outcome increments its counter."""
    diag = StreamDiagnostics()
    diag.record_outcome(
        STREAM_OUTCOME_UPSTREAM_READ_TIMEOUT,
        proxy_request_id="req-rt",
        elapsed_ms=500,
        exception_class="ReadTimeout",
    )
    snap = diag.snapshot()
    assert snap["outcomes"][STREAM_OUTCOME_UPSTREAM_READ_TIMEOUT] == 1
    assert snap["outcomes"][STREAM_OUTCOME_UPSTREAM_MIDSTREAM_ERROR] == 0


@pytest.mark.asyncio()
async def test_database_contention_includes_lock_wait_histogram() -> None:
    """``Database.contention_snapshot()`` exposes p50/p95/p99 lock waits."""
    from eggpool.db.connection import Database
    from eggpool.db.migrations import MigrationRunner

    db = Database(path=":memory:")
    await db.connect()
    try:
        runner = MigrationRunner(db)
        await runner.run()
        snap = db.contention_snapshot()
        assert "lock_wait_count" in snap
        assert "lock_wait_p50_ms" in snap
        assert "lock_wait_p95_ms" in snap
        assert "lock_wait_p99_ms" in snap
        assert "lock_wait_max_ms" in snap
        # Fresh instance: histograms reflect only what this test does.
        # Migrating populated some samples; reset for the strict-empty
        # assertion and re-acquire the lock to confirm the histogram is
        # populated.
        db._lock_wait_samples_s = collections.deque(maxlen=512)  # type: ignore[attr-defined]
        db._lock_wait_count = 0  # type: ignore[attr-defined]
        assert db.contention_snapshot()["lock_wait_sample_count"] == 0
        # Acquire the connection lock and run an operation to populate
        # the histogram with a real sample.  Use ``fetch_one`` rather
        # than ``execute_write`` so the path goes through
        # ``_connection_access`` (which records the wait sample) instead
        # of bypassing the lock inside ``transaction()``.
        await db.fetch_one("SELECT 1")
        snap = db.contention_snapshot()
        assert snap["lock_wait_sample_count"] >= 1
        assert snap["lock_wait_count"] >= 1
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_runtime_metrics_includes_stream_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``RuntimeMetricsService.snapshot()`` surfaces stream diagnostics."""
    from eggpool.runtime_metrics import RuntimeMetricsService

    diag = StreamDiagnostics()
    diag.record_outcome(STREAM_OUTCOME_COMPLETED, elapsed_ms=42)
    service = RuntimeMetricsService(
        config=_StubConfig(),
        db=_StubDB(),
        stats_db=None,
        supervisor=None,
        task_monitor=None,
        router=None,
        health_manager=None,
        started_monotonic=0.0,
        started_epoch=0.0,
        stream_diagnostics=diag,
    )
    snap = await service.snapshot()
    assert snap["stream_diagnostics"]["enabled"] is True
    assert snap["stream_diagnostics"]["outcomes"][STREAM_OUTCOME_COMPLETED] == 1


class _StubDB:
    """Minimal stub so ``_snapshot_db`` does not touch a real connection."""

    def __init__(self) -> None:
        self._conn = None

    def contention_snapshot(self) -> dict[str, Any]:
        return {
            "write_ops": 0,
            "read_ops": 0,
            "total_transactions": 0,
            "total_nested_transactions": 0,
            "last_operation_error_class": None,
            "cumulative_lock_wait_s": 0.0,
            "max_lock_wait_s": 0.0,
            "lock_wait_count": 0,
            "lock_wait_p50_ms": None,
            "lock_wait_p95_ms": None,
            "lock_wait_p99_ms": None,
            "lock_wait_max_ms": None,
            "lock_wait_sample_count": 0,
        }

    async def execute_pragma(self, _pragma: str) -> list[Any]:
        return []


class _StubConfig:
    """Minimal config stub: only ``server.threads`` and ``database`` are touched."""

    class _Server:
        threads = 2

    class _Database:
        path = ":memory:"
        wal = True
        synchronous = "NORMAL"
        busy_timeout_ms = 5000
        worker_threads = 2

    class _Routing:
        class _Trace:
            mode = "sampled"

        trace = _Trace()

    class _Metrics:
        write_mode = "immediate"

    server = _Server()
    database = _Database()
    routing = _Routing()
    metrics = _Metrics()
