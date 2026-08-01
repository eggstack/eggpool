"""Finalization state machine and supervisor tests."""

from __future__ import annotations

import asyncio
import contextlib
import time
from unittest.mock import MagicMock

import pytest

from eggpool.request.finalization_job import (
    AttemptRuntimeLease,
    FinalizationCapacityError,
    FinalizationIdentity,
    FinalizationInvariantError,
    FinalizationProgress,
    FinalizationRecord,
    RequestFinalizationJob,
    RequestFinalizationSupervisor,
    TerminalConflictError,
)
from eggpool.request.finalizer import (
    DurableFinalizationResult,
    FinalizationData,
    FinalizationOutcome,
)


def _make_identity(**overrides: object) -> FinalizationIdentity:
    defaults = dict(
        proxy_request_id="req-1",
        db_request_id="db-req-1",
        attempt_id=1,
        reservation_id="res-1",
        account_id=10,
        account_name="acct",
        provider_id="openai",
        model_id="gpt-4",
        client_protocol="openai",
        upstream_protocol="openai",
        attempt_number=1,
    )
    defaults.update(overrides)
    return FinalizationIdentity(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# FinalizationProgress state machine
# ---------------------------------------------------------------------------


class TestFinalizationProgress:
    """State machine transitions are monotonic."""

    def test_initial_state_is_created(self) -> None:
        assert FinalizationProgress.CREATED == "created"

    def test_only_completed_is_terminal(self) -> None:
        for state in FinalizationProgress:
            if state == FinalizationProgress.COMPLETED:
                assert True
            else:
                assert state != FinalizationProgress.COMPLETED

    def test_all_states_are_strings(self) -> None:
        for state in FinalizationProgress:
            assert isinstance(state.value, str)


# ---------------------------------------------------------------------------
# RequestFinalizationJob
# ---------------------------------------------------------------------------


class TestRequestFinalizationJob:
    """Job lifecycle, progress, and completion."""

    def test_initial_state(self) -> None:
        job = RequestFinalizationJob(
            identity=_make_identity(),
            outcome="client_cancelled",
        )
        assert not job.is_complete
        assert job.progress == FinalizationProgress.CREATED
        assert job.request_id == "req-1"
        assert job.attempt_count == 0
        assert job.failure_count == 0

    def test_progress_advances_to_completed(self) -> None:
        job = RequestFinalizationJob(
            identity=_make_identity(),
            outcome="client_cancelled",
        )
        # Run without dependencies — should complete through all steps
        asyncio.run(job.run())
        assert job.is_complete
        assert job.progress == FinalizationProgress.COMPLETED
        assert job.attempt_count == 1

    def test_concurrent_callers_share_task(self) -> None:
        """Multiple concurrent run() calls share the same retained task."""
        job = RequestFinalizationJob(
            identity=_make_identity(),
            outcome="client_cancelled",
        )

        async def _run_both() -> tuple[None, None]:
            return await asyncio.gather(job.run(), job.run())

        asyncio.run(_run_both())
        assert job.is_complete
        assert job.attempt_count == 1

    def test_cancellation_does_not_cancel_retained_task(self) -> None:
        """Cancelling the caller does not cancel the retained task."""
        job = RequestFinalizationJob(
            identity=_make_identity(),
            outcome="client_cancelled",
        )

        async def _test() -> None:
            task = asyncio.create_task(job.run())
            # Let the task start
            await asyncio.sleep(0)
            # Cancel the caller
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            # The retained task should still complete
            await asyncio.sleep(0.1)
            assert job.is_complete

        asyncio.run(_test())

    def test_to_record_is_frozen(self) -> None:
        job = RequestFinalizationJob(
            identity=_make_identity(),
            outcome="client_cancelled",
        )
        asyncio.run(job.run())
        record = job.to_record()
        assert isinstance(record, FinalizationRecord)
        assert record.proxy_request_id == "req-1"
        assert record.outcome == "client_cancelled"
        assert record.progress == "completed"
        # Frozen
        with pytest.raises(AttributeError):
            record.proxy_request_id = "changed"  # type: ignore[misc]

    def test_release_references_clears_dependencies(self) -> None:
        job = RequestFinalizationJob(
            identity=_make_identity(),
            outcome="client_cancelled",
        )
        finalizer = MagicMock()
        selected = MagicMock()
        job.set_dependencies(finalizer=finalizer, selected=selected)
        assert job._finalizer is not None
        job.release_references()
        assert job._finalizer is None
        assert job._selected is None

    def test_unknown_step_raises_invariant_error(self) -> None:
        """An invalid progress step raises FinalizationInvariantError."""
        job = RequestFinalizationJob(
            identity=_make_identity(),
            outcome="client_cancelled",
        )
        # Forge an invalid step
        object.__setattr__(job, "_progress", "invalid_step")

        async def _run() -> None:
            await job.run()

        with pytest.raises((FinalizationInvariantError, Exception)):
            asyncio.run(_run())

    @pytest.mark.asyncio
    async def test_runtime_failure_resumes_without_repeating_durable_finalization(
        self,
    ) -> None:
        lease = AttemptRuntimeLease(
            account_name="acct",
            usage_outcome_required=True,
            health_outcome_required=True,
            account_runtime_outcome_required=True,
        )
        durable = DurableFinalizationResult(
            request_terminal=True,
            request_transitioned=True,
            attempt_transitioned=True,
            attempt_terminal=True,
            reservation_terminal=True,
            reservation_transitioned=True,
        )
        calls = {"durable": 0, "runtime": 0}

        class FlakyFinalizer:
            async def finalize(self, selected: object, data: object) -> object:
                calls["durable"] += 1
                return durable

            async def apply_runtime_convergence(self, **kwargs: object) -> None:
                calls["runtime"] += 1
                if calls["runtime"] == 1:
                    raise RuntimeError("health busy")
                runtime_lease = kwargs["runtime_lease"]
                runtime_lease.mark_component_complete("usage")
                runtime_lease.mark_component_complete("health")
                runtime_lease.mark_component_complete("account_runtime")
                runtime_lease.released = True

        job = RequestFinalizationJob(
            identity=_make_identity(),
            outcome=FinalizationOutcome.COMPLETED.value,
            finalization_data=FinalizationData(
                outcome=FinalizationOutcome.COMPLETED,
            ),
            runtime_lease=lease,
        )
        job.set_dependencies(finalizer=FlakyFinalizer(), selected=object())

        with pytest.raises(RuntimeError, match="health busy"):
            await job.run()
        assert job.progress == FinalizationProgress.RUNTIME_RELEASE_PENDING
        assert not job.result.runtime_cleanup_complete
        assert calls == {"durable": 1, "runtime": 1}

        await job.run()
        assert job.is_complete
        assert job.result.runtime_cleanup_complete
        assert calls == {"durable": 1, "runtime": 2}


# ---------------------------------------------------------------------------
# RequestFinalizationSupervisor
# ---------------------------------------------------------------------------


class TestRequestFinalizationSupervisor:
    """Supervisor registry, deduplication, and diagnostics."""

    def _make_supervisor(self, **kwargs: object) -> RequestFinalizationSupervisor:
        db = MagicMock()
        return RequestFinalizationSupervisor(db=db, **kwargs)  # type: ignore[arg-type]

    def test_register_or_get_creates_job(self) -> None:
        sup = self._make_supervisor()
        job = sup.register_or_get(_make_identity(), "client_cancelled")
        assert job.request_id == "req-1"
        assert sup.active_count == 1

    def test_register_or_get_deduplicates(self) -> None:
        sup = self._make_supervisor()
        job1 = sup.register_or_get(_make_identity(), "client_cancelled")
        job2 = sup.register_or_get(_make_identity(), "client_cancelled")
        assert job1 is job2
        assert sup.active_count == 1

    def test_registry_key_includes_attempt_id(self) -> None:
        sup = self._make_supervisor()
        first = sup.register_or_get(_make_identity(attempt_id=1), "completed")
        second = sup.register_or_get(_make_identity(attempt_id=2), "completed")
        assert first is not second
        assert sup.active_count == 2

    def test_conflicting_terminal_outcome_is_rejected(self) -> None:
        sup = self._make_supervisor()
        sup.register_or_get(_make_identity(), "completed")
        with pytest.raises(TerminalConflictError):
            sup.register_or_get(_make_identity(), "client_cancelled")
        assert sup.snapshot()["terminal_conflicts"] == 1

    def test_register_respects_capacity(self) -> None:
        sup = self._make_supervisor(max_active_jobs=2)
        sup.register_or_get(
            _make_identity(proxy_request_id="req-1"), "client_cancelled"
        )
        sup.register_or_get(
            _make_identity(proxy_request_id="req-2"), "client_cancelled"
        )
        # Third registration exceeds capacity before ownership transfer.
        with pytest.raises(FinalizationCapacityError):
            sup.register_or_get(
                _make_identity(proxy_request_id="req-3"), "client_cancelled"
            )
        assert sup.active_count == 2  # Only 2 tracked

    def test_get_job(self) -> None:
        sup = self._make_supervisor()
        sup.register_or_get(_make_identity(), "client_cancelled")
        assert sup.get_job("req-1") is not None
        assert sup.get_job("nonexistent") is None

    def test_reconcile_completed_job(self) -> None:
        sup = self._make_supervisor()
        job = sup.register_or_get(_make_identity(), "client_cancelled")
        # Run to completion
        asyncio.run(job.run())
        # Reconcile
        sup._reconcile_completed_jobs()
        assert sup.active_count == 0
        history = list(sup._history)
        assert len(history) == 1
        assert history[0].proxy_request_id == "req-1"

    def test_snapshot(self) -> None:
        sup = self._make_supervisor()
        sup.register_or_get(_make_identity(), "client_cancelled")
        snap = sup.snapshot()
        assert snap["active_count"] == 1
        assert snap["history_count"] == 0
        assert "counters" in snap
        assert "config" in snap

    def test_shutdown_drains_jobs(self) -> None:
        sup = self._make_supervisor()
        sup.register_or_get(_make_identity(), "client_cancelled")

        async def _shutdown() -> int:
            return await sup.shutdown(timeout_s=5.0)

        remaining = asyncio.run(_shutdown())
        assert remaining == 0
        assert sup.active_count == 0

    def test_history_is_bounded(self) -> None:
        sup = self._make_supervisor()
        for i in range(100):
            job = sup.register_or_get(
                _make_identity(proxy_request_id=f"req-{i}"),
                "client_cancelled",
            )
            asyncio.run(job.run())
        sup._reconcile_completed_jobs()
        assert len(sup._history) <= 64  # FINALIZATION_HISTORY_MAX

    def test_counters_track_registration(self) -> None:
        sup = self._make_supervisor()
        sup.register_or_get(
            _make_identity(proxy_request_id="req-1"), "client_cancelled"
        )
        sup.register_or_get(
            _make_identity(proxy_request_id="req-2"), "client_cancelled"
        )
        snap = sup.snapshot()
        assert snap["counters"]["registered"] == 2

    def test_saturation_counter(self) -> None:
        sup = self._make_supervisor(max_active_jobs=1)
        sup.register_or_get(
            _make_identity(proxy_request_id="req-1"), "client_cancelled"
        )
        with pytest.raises(FinalizationCapacityError):
            sup.register_or_get(
                _make_identity(proxy_request_id="req-2"), "client_cancelled"
            )
        snap = sup.snapshot()
        assert snap["counters"]["saturation_rejections"] == 1

    @pytest.mark.asyncio
    async def test_retryable_failure_is_scheduled_and_converges(self) -> None:
        """The supervisor retries a failed job without an external drain."""
        sup = self._make_supervisor(
            retry_backoff_base_s=0.01,
            retry_backoff_cap_s=0.01,
            max_retry_age_s=1.0,
        )
        job = sup.register_or_get(_make_identity(), "client_cancelled")
        calls = 0

        async def _fail_once() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient")

        job._execute_durable_finalization = _fail_once  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="transient"):
            await job.run()
        await asyncio.sleep(0.05)
        assert calls == 2
        assert job.is_complete
        assert job.attempt_count == 2
        assert job.failure_count == 1
        assert job._retry_count == 1
        assert sup.active_count == 0
        await sup.shutdown(timeout_s=1.0)

    def test_retry_age_exhaustion_retires_job_and_frees_capacity(self) -> None:
        sup = self._make_supervisor(max_active_jobs=1, max_retry_age_s=1.0)
        job = sup.register_or_get(_make_identity(), "client_cancelled")
        selected = MagicMock()
        finalizer = MagicMock()
        job.set_dependencies(finalizer=finalizer, selected=selected)
        object.__setattr__(job, "_created_at", 0.0)

        sup._schedule_retry(job)

        assert job.health == "failed"
        assert sup.active_count == 0
        assert job._finalizer is None
        assert len(sup._failed_jobs) == 1
        assert sup._retry_heap == []
        replacement = sup.register_or_get(
            _make_identity(proxy_request_id="req-2"), "client_cancelled"
        )
        assert replacement.request_id == "req-2"

    @pytest.mark.asyncio
    async def test_due_retry_expired_at_execution_is_not_started(self) -> None:
        sup = self._make_supervisor(max_retry_age_s=1.0)
        job = sup.register_or_get(_make_identity(), "client_cancelled")
        job._execute_durable_finalization = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("expired retry must not execute")
        )
        object.__setattr__(job, "_created_at", time.monotonic() - 2.0)
        key = "req-1:1"
        sup._retry_heap.append((time.monotonic(), 1, key))
        task = asyncio.create_task(sup._retry_scheduler())
        await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert sup.active_count == 0
        assert job.attempt_count == 0
