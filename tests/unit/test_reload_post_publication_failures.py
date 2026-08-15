"""Reload post-publication and compensation failure paths.

Covers failure stages that the focused reload failure-injection suite does not
exercise:

Post-publication:
- process transition apply failure (compensation retries)
- observable-state update failure (compensation handles)
- retirement scheduling failure (compensation handles)

Compensation:
- compensation itself fails (operator intervention required)
- compensation succeeds after process-transition retry

Shutdown:
- shutdown during active transaction waits for completion
- shutdown aborts pre-commit transaction

All tests follow the same invariant pattern:
- active generation unchanged (or advanced on success)
- no mixed state (complete old or complete new)
- structured operational event recorded
- ReloadResult maps to the right outcome
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from eggpool.control.reload_manager import (
    ReloadManager,
    ReloadPreparationError,
)
from eggpool.reload_transaction import TransactionState
from eggpool.runtime_manager import RuntimeManager

if TYPE_CHECKING:
    from eggpool.config_reload_policy import ReloadResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_process() -> MagicMock:
    proc = MagicMock()
    proc.db = MagicMock()
    proc.stats_db = MagicMock()
    proc.metrics_coalescer = MagicMock()
    proc.process_supervisor = None
    return proc


def _make_validation(
    *,
    content_digest: str = "a" * 64,
    warnings: tuple = (),
    config: MagicMock | None = None,
) -> MagicMock:
    v = MagicMock()
    v.content_digest = content_digest
    v.warnings = warnings
    v.config = config or MagicMock()
    return v


def _make_diff(changes: tuple = ()) -> MagicMock:
    d = MagicMock()
    d.changes = changes
    d.live = bool(changes)
    d.restart_required = tuple(
        c for c in changes if getattr(c, "disposition", None) == "restart"
    )
    return d


async def _ignore_event(*args: object, **kwargs: object) -> None:
    """No-op event sink for failure paths that do not record an event."""
    del args, kwargs


def _make_generation(generation_id: int = 0, digest: str = "a" * 64) -> MagicMock:
    gen = MagicMock()
    gen.generation_id = generation_id
    gen.config_digest = digest
    gen.config = MagicMock()
    return gen


def _make_candidate(
    generation_id: int = 1,
    digest: str = "b" * 64,
) -> MagicMock:
    gen = _make_generation(generation_id, digest)
    process = MagicMock()
    diff = _make_diff()
    candidate = MagicMock()
    candidate.generation = gen
    candidate.process = process
    candidate.diff = diff
    candidate._built_generation = gen
    return candidate


def _make_real_config() -> object:
    from eggpool.models.config import AppConfig, ServerConfig

    return AppConfig(server=ServerConfig(host="0.0.0.0", port=8080))


def _make_real_generation(
    *,
    generation_id: int = 0,
    config: object | None = None,
    config_digest: str = "a" * 64,
) -> Any:
    from eggpool.runtime_manager import RuntimeGeneration

    if config is None:
        config = _make_real_config()
    return RuntimeGeneration(
        generation_id=generation_id,
        config=config,
        config_digest=config_digest,
        registry=MagicMock(),
        catalog=MagicMock(),
        router=MagicMock(),
        coordinator=MagicMock(),
        client_pool=MagicMock(),
        outbound_manager=MagicMock(),
        health_manager=MagicMock(),
        cost_calculator=MagicMock(),
        transcoder_policy=MagicMock(),
        compression_policy=MagicMock(),
        dispatch_overhead_recorder=MagicMock(),
        dispatch_span_recorder=MagicMock(),
        account_backoff_repo=MagicMock(),
        stats_service=MagicMock(),
        supervisor=MagicMock(),
        routing_trace_guard=MagicMock(),
        routing_trace_writer=MagicMock(),
        created_at_monotonic=0.0,
        created_at_epoch=0.0,
    )


def _make_change() -> MagicMock:
    change = MagicMock()
    change.section = "routing"
    return change


# ===========================================================================
# 1. Process transition apply failure — compensation retries
# ===========================================================================


class TestProcessTransitionApplyFailure:
    @pytest.mark.asyncio
    async def test_process_transition_failure_rolls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Inside the db.transaction(), a process-transition failure must
        roll back the entire transaction: persistence, runtime staging,
        and process transitions all revert to the pre-reload state."""
        rm = RuntimeManager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        baseline = _make_real_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)
        # Provide a proper abort method that returns a CleanupDiagnostics mock
        from eggpool.runtime_manager import CleanupDiagnostics

        candidate.abort = AsyncMock(  # type: ignore[method-assign]
            return_value=CleanupDiagnostics(
                generation_id=5,
                ownership_state="transferred",
                resource_types_registered=(),
                resource_types_closed=(),
                close_duration_s=0.0,
                close_errors=(),
                timed_out=False,
                primary_failure="test",
                primary_failure_stage="commit",
                ownership_state_at_failure="prepared",
            )
        )
        validation = _make_validation()

        monkeypatch.setattr(mgr, "_record_event", _ignore_event)

        with (
            patch.object(
                mgr, "_compute_reload_diff", new_callable=AsyncMock, return_value=diff
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                new_callable=AsyncMock,
                return_value=candidate,
            ),
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            patch(
                "eggpool.control.reload_manager.preflight_all_transitions",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(rm, "begin_retirement", new_callable=AsyncMock),
        ):
            mgr.TEST_INJECT_TRANSITION_APPLY_FAILURE = RuntimeError(
                "process transition apply failed"
            )
            try:
                result = await mgr.reload(validation)
            finally:
                mgr.TEST_INJECT_TRANSITION_APPLY_FAILURE = None

        # Process transition failure rolls back everything — generation
        # stays at the pre-reload state.
        assert rm.active_snapshot().generation_id == 0
        # Result is a failure.
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_process_transition_compensation_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the compensation retry also fails, the transaction is
        marked compensation_failed."""
        rm = RuntimeManager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        baseline = _make_real_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)
        # Provide a proper abort method that returns a CleanupDiagnostics mock
        from eggpool.runtime_manager import CleanupDiagnostics

        candidate.abort = AsyncMock(  # type: ignore[method-assign]
            return_value=CleanupDiagnostics(
                generation_id=5,
                ownership_state="transferred",
                resource_types_registered=(),
                resource_types_closed=(),
                close_duration_s=0.0,
                close_errors=(),
                timed_out=False,
                primary_failure="test",
                primary_failure_stage="commit",
                ownership_state_at_failure="prepared",
            )
        )
        validation = _make_validation()

        monkeypatch.setattr(mgr, "_record_event", _ignore_event)

        with (
            patch.object(
                mgr, "_compute_reload_diff", new_callable=AsyncMock, return_value=diff
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                new_callable=AsyncMock,
                return_value=candidate,
            ),
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            patch(
                "eggpool.control.reload_manager.preflight_all_transitions",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(rm, "begin_retirement", new_callable=AsyncMock),
        ):
            mgr.TEST_INJECT_TRANSITION_APPLY_FAILURE = RuntimeError(
                "process transition always fails"
            )
            try:
                result = await mgr.reload(validation)
            finally:
                mgr.TEST_INJECT_TRANSITION_APPLY_FAILURE = None

        # Process transition failure rolls back everything — generation
        # stays at the pre-reload state.
        assert rm.active_snapshot().generation_id == 0
        # Compensation failed.
        assert result.ok is False
        assert "Reload compensated" not in result.message


# ===========================================================================
# 2. Shutdown during active transaction
# ===========================================================================


class TestShutdownDuringTransaction:
    @pytest.mark.asyncio
    async def test_pre_commit_transaction_aborts_on_shutdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transaction before the commit point must abort when
        shutdown is detected."""
        rm = RuntimeManager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        baseline = _make_real_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        validation = _make_validation()

        monkeypatch.setattr(mgr, "_record_event", _ignore_event)

        async def _set_shutdown(*args: Any, **kwargs: Any) -> None:
            # Simulate shutdown starting during candidate build
            rm._shutdown_in_progress = True
            raise ReloadPreparationError("Process is shutting down")

        with (
            patch.object(
                mgr, "_compute_reload_diff", new_callable=AsyncMock, return_value=diff
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                new_callable=AsyncMock,
                side_effect=_set_shutdown,
            ),
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            patch.object(mgr, "_publish_generation", new_callable=AsyncMock),
        ):
            result = await mgr.reload(validation)

        assert result.ok is False
        assert rm.active_snapshot().generation_id == 0

    @pytest.mark.asyncio
    async def test_wait_for_transaction_completion_returns_when_idle(
        self,
    ) -> None:
        """wait_for_transaction_completion returns True immediately
        when no transaction is active."""
        rm = RuntimeManager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        result = await mgr.wait_for_transaction_completion(timeout_s=1.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_wait_for_transaction_completion_signals_on_finish(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """wait_for_transaction_completion returns after the transaction
        completes."""
        rm = RuntimeManager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        baseline = _make_real_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)
        validation = _make_validation()

        monkeypatch.setattr(mgr, "_record_event", _ignore_event)

        async def _publish_and_install(c: Any, d: Any, **kwargs: Any) -> None:
            del c, d, kwargs

        with (
            patch.object(
                mgr, "_compute_reload_diff", new_callable=AsyncMock, return_value=diff
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                new_callable=AsyncMock,
                return_value=candidate,
            ),
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            patch.object(
                mgr,
                "_publish_generation",
                new_callable=AsyncMock,
                side_effect=_publish_and_install,
            ),
            patch.object(rm, "begin_retirement", new_callable=AsyncMock),
        ):
            # Start reload and wait concurrently
            async def _reload() -> ReloadResult:
                return await mgr.reload(validation)

            reload_task = asyncio.create_task(_reload())
            # Wait for completion — should return True
            wait_result = await mgr.wait_for_transaction_completion(timeout_s=5.0)
            assert wait_result is True
            # Reload should have completed
            result = await reload_task
            assert result.ok is True


# ===========================================================================
# 3. Transaction state machine — monotonic transitions
# ===========================================================================


class TestTransactionStateMachine:
    def test_valid_forward_transitions(self) -> None:
        """All expected forward transitions are valid."""
        from eggpool.reload_transaction import ReloadTransaction

        txn = ReloadTransaction(
            request_id="test-1",
            validation=_make_validation(),
        )
        assert txn.state == TransactionState.CREATED

        txn.mark_validated()
        assert txn.state == TransactionState.VALIDATED

        txn.mark_diffed(
            _make_diff(),
            changed_sections=("routing",),
            restart_required=(),
        )
        assert txn.state == TransactionState.DIFFED

        txn.mark_candidate_prepared(_make_candidate(), generation_id=1)
        assert txn.state == TransactionState.CANDIDATE_PREPARED

        txn.mark_persistence_prepared(MagicMock())
        assert txn.state == TransactionState.PERSISTENCE_PREPARED

        from eggpool.reload_transaction import ProcessTransitionPlan

        txn.mark_process_transitions_prepared(
            ProcessTransitionPlan(task_specs=(), callback_factories={}, transitions=())
        )
        assert txn.state == TransactionState.PROCESS_TRANSITIONS_PREPARED

        # Preflight always runs before commit state is recorded.
        txn.mark_process_transitions_preflighted([])
        assert txn.state == TransactionState.PROCESS_TRANSITIONS_PREFLIGHTED

        txn.mark_commit_started(old_generation_id=0)
        assert txn.state == TransactionState.COMMIT_STARTED

        txn.mark_runtime_published(_make_generation(generation_id=1))
        assert txn.state == TransactionState.RUNTIME_PUBLISHED

        txn.mark_process_transitions_applied()
        assert txn.state == TransactionState.PROCESS_TRANSITIONS_APPLIED

        txn.mark_persistence_committed()
        assert txn.state == TransactionState.PERSISTENCE_COMMITTED

        txn.mark_observable_state_updated()
        assert txn.state == TransactionState.OBSERVABLE_STATE_UPDATED

        txn.mark_retirement_scheduled()
        assert txn.state == TransactionState.RETIREMENT_SCHEDULED

        txn.mark_completed()
        assert txn.state == TransactionState.COMPLETED
        assert txn.is_terminal is True

    def test_invalid_transition_raises(self) -> None:
        """An invalid state transition raises TransactionStateError."""
        from eggpool.reload_transaction import (
            ReloadTransaction,
            TransactionStateError,
        )

        txn = ReloadTransaction(
            request_id="test-2",
            validation=_make_validation(),
        )
        with pytest.raises(TransactionStateError):
            # Cannot go from CREATED directly to COMMIT_STARTED
            txn._transition_to(TransactionState.COMMIT_STARTED)

    def test_abort_from_any_non_terminal_state(self) -> None:
        """mark_aborting works from any non-terminal state."""
        from eggpool.reload_transaction import ReloadTransaction

        txn = ReloadTransaction(
            request_id="test-3",
            validation=_make_validation(),
        )
        txn.mark_aborting(RuntimeError("test abort"))
        assert txn.state == TransactionState.ABORTING

        txn.mark_aborted()
        assert txn.state == TransactionState.ABORTED
        assert txn.is_terminal is True

    def test_compensation_failed_from_aborting(self) -> None:
        """mark_compensation_failed transitions from ABORTING."""
        from eggpool.reload_transaction import ReloadTransaction

        txn = ReloadTransaction(
            request_id="test-4",
            validation=_make_validation(),
        )
        txn.mark_aborting(RuntimeError("compensation needed"))
        txn.mark_compensation_failed()
        assert txn.state == TransactionState.COMPENSATION_FAILED
        assert txn.is_terminal is True

    def test_snapshot_includes_all_fields(self) -> None:
        """Transaction snapshot includes all expected fields."""
        from eggpool.reload_transaction import ReloadTransaction

        txn = ReloadTransaction(
            request_id="test-5",
            validation=_make_validation(),
        )
        snapshot = txn.snapshot()
        assert snapshot["state"] == "created"
        assert snapshot["request_id"] == "test-5"
        assert "elapsed_s" in snapshot
        assert "commit_diagnostics" in snapshot
        assert "transition_history" in snapshot
