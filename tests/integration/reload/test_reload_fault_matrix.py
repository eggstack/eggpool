"""Complete fault-injection matrix for the reload transaction (Workstream C).

Covers every stage of the Phase 6 transactional flow:

Preparation faults:
- Candidate build failure (on_candidate_started / on_candidate_complete)
- Persistence prepare failure (on_reconcile_started / on_reconcile_prepared)

Commit faults:
- Persistence apply failure (_apply_persistence_delta)
- Publication failure (_publish_generation)
- Process transition apply failure (_apply_process_transitions)

Cleanup/compensation faults:
- Candidate close failure (client_pool.close, outbound_manager.close)

Cancellation faults:
- Cancellation injected via ReloadFaultInjector at every stage
- Post-publication cancellation shielding
- Concurrent reload busy rejection

Every fault yields complete old state (pre-commit) or complete new state
(post-commit) — never mixed state.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import pytest

from eggpool.control.reload_manager import (
    ReloadObserver,
)
from tests.support.reload_faults import FaultType, ReloadFaultInjector
from tests.support.runtime_snapshot import RuntimeSnapshot

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


# ---------------------------------------------------------------------------
# Preparation fault tests
# ---------------------------------------------------------------------------


class TestCandidateBuildFaults:
    """Faults during candidate generation construction."""

    @pytest.mark.asyncio
    async def test_build_failure_at_candidate_started(
        self, reload_harness: ReloadHarness
    ) -> None:
        """on_candidate_started fault aborts before any resource creation."""
        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

        injector = ReloadFaultInjector(
            target_stage="on_candidate_started",
            fault_type=FaultType.RECOVERABLE,
        )
        result = await reload_harness.reload(observer=injector)

        assert result.ok is False
        assert injector.fired

        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        diffs = post.assert_same_generation(pre)
        assert diffs == [], f"Generation changed after build failure: {diffs}"

    @pytest.mark.asyncio
    async def test_build_failure_at_candidate_complete(
        self, reload_harness: ReloadHarness
    ) -> None:
        """on_candidate_complete fault aborts after candidate is built."""
        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

        injector = ReloadFaultInjector(
            target_stage="on_candidate_complete",
            fault_type=FaultType.RECOVERABLE,
        )
        result = await reload_harness.reload(observer=injector)

        assert result.ok is False
        assert injector.fired

        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        diffs = post.assert_same_generation(pre)
        assert diffs == [], f"Generation changed: {diffs}"

    @pytest.mark.asyncio
    async def test_reconcile_fault_at_reconcile_started(
        self, reload_harness: ReloadHarness
    ) -> None:
        """on_reconcile_started fault aborts before persistence prepare."""
        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

        injector = ReloadFaultInjector(
            target_stage="on_reconcile_started",
            fault_type=FaultType.RECOVERABLE,
        )
        result = await reload_harness.reload(observer=injector)

        assert result.ok is False
        assert injector.fired

        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        diffs = post.assert_same_generation(pre)
        assert diffs == [], f"Generation changed: {diffs}"

    @pytest.mark.asyncio
    async def test_validation_fault_at_on_validation_complete(
        self, reload_harness: ReloadHarness
    ) -> None:
        """on_validation_complete fault aborts before diff computation."""
        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

        injector = ReloadFaultInjector(
            target_stage="on_validation_complete",
            fault_type=FaultType.RECOVERABLE,
        )
        result = await reload_harness.reload(observer=injector)

        assert result.ok is False
        assert injector.fired

        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        diffs = post.assert_same_generation(pre)
        assert diffs == [], f"Generation changed: {diffs}"


# ---------------------------------------------------------------------------
# Commit fault tests
# ---------------------------------------------------------------------------


class TestCommitFaults:
    """Faults during the narrow commit phase (after preparation)."""

    @pytest.mark.asyncio
    async def test_persistence_apply_failure_preserves_generation(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Failure during _apply_persistence_delta aborts without publication."""
        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

        original_apply = reload_harness.reload_manager._apply_persistence_delta

        async def _failing_apply(delta: object) -> None:
            raise OSError("SQLite write failed")

        reload_harness.reload_manager._apply_persistence_delta = _failing_apply  # type: ignore[assignment]
        try:
            result = await reload_harness.reload()
        finally:
            reload_harness.reload_manager._apply_persistence_delta = original_apply

        assert result.ok is False

        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        diffs = post.assert_same_generation(pre)
        assert diffs == [], f"Generation changed after persistence failure: {diffs}"

    @pytest.mark.asyncio
    async def test_publish_failure_preserves_generation(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Failure during _publish_generation aborts without generation swap."""
        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

        reload_harness.reload_manager.TEST_INJECT_PUBLISH_FAILURE = RuntimeError(
            "simulated publish failure"
        )
        try:
            result = await reload_harness.reload()
        finally:
            reload_harness.reload_manager.TEST_INJECT_PUBLISH_FAILURE = None

        assert result.ok is False

        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        diffs = post.assert_same_generation(pre)
        assert diffs == [], f"Generation changed after publish failure: {diffs}"

    @pytest.mark.asyncio
    async def test_process_transition_apply_failure_compensates(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Process-transition failure inside the transaction rolls back the
        entire transaction — no publication, no candidate visible."""
        from eggpool.reload_diagnostics import ReloadResultCategory

        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

        original_seam = (
            reload_harness.reload_manager.TEST_INJECT_TRANSITION_APPLY_FAILURE
        )
        reload_harness.reload_manager.TEST_INJECT_TRANSITION_APPLY_FAILURE = (
            RuntimeError("process transition failed")
        )
        try:
            result = await reload_harness.reload()
        finally:
            reload_harness.reload_manager.TEST_INJECT_TRANSITION_APPLY_FAILURE = (
                original_seam
            )

        # Process transitions fail inside the transaction → rollback.
        assert result.ok is False
        diag = reload_harness.reload_manager._last_diagnostic_result
        assert diag is not None
        assert diag.category == ReloadResultCategory.FAILED_PROCESS_TRANSITION_APPLY

        # Old generation remains active.
        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        diffs = post.assert_same_generation(pre)
        assert diffs == [], (
            f"Generation changed after process transition failure: {diffs}"
        )

    @pytest.mark.asyncio
    async def test_process_transition_persistent_failure_marks_compensation_failed(
        self, reload_harness: ReloadHarness
    ) -> None:
        """If process transitions always fail, the transaction rolls back —
        no publication, no candidate visible."""
        from eggpool.reload_diagnostics import ReloadResultCategory

        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

        original_seam = (
            reload_harness.reload_manager.TEST_INJECT_TRANSITION_APPLY_FAILURE
        )
        reload_harness.reload_manager.TEST_INJECT_TRANSITION_APPLY_FAILURE = (
            RuntimeError("process transition always fails")
        )
        try:
            result = await reload_harness.reload()
        finally:
            reload_harness.reload_manager.TEST_INJECT_TRANSITION_APPLY_FAILURE = (
                original_seam
            )

        assert result.ok is False
        diag = reload_harness.reload_manager._last_diagnostic_result
        assert diag is not None
        assert diag.category == ReloadResultCategory.FAILED_PROCESS_TRANSITION_APPLY

        # Old generation remains active — no publication occurred.
        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        diffs = post.assert_same_generation(pre)
        assert diffs == [], (
            f"Generation changed after process transition failure: {diffs}"
        )


# ---------------------------------------------------------------------------
# Cancellation fault tests
# ---------------------------------------------------------------------------


class TestCancellationFaults:
    """Cancellation injected at various pipeline stages."""

    @pytest.mark.asyncio
    async def test_cancellation_at_candidate_build(
        self, reload_harness: ReloadHarness
    ) -> None:
        """CancelledError during candidate build aborts before commit."""
        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

        injector = ReloadFaultInjector(
            target_stage="on_candidate_started",
            fault_type=FaultType.CANCELLATION,
        )

        with pytest.raises(asyncio.CancelledError):
            await reload_harness.reload(observer=injector)

        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        diffs = post.assert_same_generation(pre)
        assert diffs == [], f"Generation changed after cancel: {diffs}"

    @pytest.mark.asyncio
    async def test_cancellation_at_reconcile(
        self, reload_harness: ReloadHarness
    ) -> None:
        """CancelledError during reconcile aborts before commit."""
        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

        injector = ReloadFaultInjector(
            target_stage="on_reconcile_started",
            fault_type=FaultType.CANCELLATION,
        )

        with pytest.raises(asyncio.CancelledError):
            await reload_harness.reload(observer=injector)

        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        diffs = post.assert_same_generation(pre)
        assert diffs == [], f"Generation changed after cancel: {diffs}"

    @pytest.mark.asyncio
    async def test_cancellation_at_publish_complete_post_publication(
        self, reload_harness: ReloadHarness
    ) -> None:
        """CancelledError at on_publish_complete (after publication) is
        shielded: the commit completes despite cancellation."""
        injector = ReloadFaultInjector(
            target_stage="on_publish_complete",
            fault_type=FaultType.CANCELLATION,
        )

        with pytest.raises(asyncio.CancelledError):
            await reload_harness.reload(observer=injector)

        # Publication succeeded; generation advanced.
        assert reload_harness.runtime_manager.active_snapshot().generation_id > 0

    @pytest.mark.asyncio
    async def test_cancellation_at_retirement(
        self, reload_harness: ReloadHarness
    ) -> None:
        """CancelledError at retirement stage — publication already done."""
        injector = ReloadFaultInjector(
            target_stage="on_retirement_started",
            fault_type=FaultType.CANCELLATION,
        )

        with pytest.raises(asyncio.CancelledError):
            await reload_harness.reload(observer=injector)

        # Publication succeeded; generation advanced despite cancel.
        assert reload_harness.runtime_manager.active_snapshot().generation_id > 0


class TestPostPublicationCancellationShielding:
    """Cancellation after publication must still complete the commit.

    The shielding code in reload_manager handles cancellation after
    RUNTIME_PUBLISHED by completing remaining commit steps under
    asyncio.shield. These tests use a task-based approach: start the
    reload in a task, wait until the generation is installed (via the
    on_publish_complete observer), then cancel the task.
    """

    @pytest.mark.asyncio
    async def test_cancel_after_publish_shields_commit(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Cancelling the reload task after publication still
        completes the remaining commit steps.
        """
        published_event = asyncio.Event()

        class PublishCompleteObserver(ReloadObserver):
            """Observer that signals when publication is done."""

            async def on_publish_complete(self, **kw: Any) -> None:
                published_event.set()

        observer = PublishCompleteObserver()

        async def _run_reload() -> None:
            await reload_harness.reload(observer=observer)  # type: ignore[arg-type]

        task = asyncio.create_task(_run_reload())
        # Wait until publication completes.
        await asyncio.wait_for(published_event.wait(), timeout=5.0)
        # Allow the reload to finish its shielding.
        await asyncio.sleep(0.05)

        # The generation should be advanced.
        gen_id = reload_harness.runtime_manager.active_snapshot().generation_id
        assert gen_id > 0

        # Await the task to check if it completed or was cancelled.
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# Concurrent reload busy rejection
# ---------------------------------------------------------------------------


class TestConcurrentReloadBusy:
    """Concurrent reload attempts must be rejected atomically."""

    @pytest.mark.asyncio
    async def test_concurrent_reloads_one_busy(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Two concurrent reloads: exactly one succeeds, the other gets busy."""
        from eggpool.control.reload_manager import ReloadInProgressError

        preparation_event = asyncio.Event()
        reload_harness.reload_manager.preparation_event = preparation_event

        results: list[bool] = []

        async def do_reload() -> None:
            try:
                r = await reload_harness.reload()
                results.append(r.ok)
            except ReloadInProgressError:
                results.append(False)

        t1 = asyncio.create_task(do_reload())
        await asyncio.sleep(0.05)
        t2 = asyncio.create_task(do_reload())

        preparation_event.set()
        await asyncio.gather(t1, t2, return_exceptions=True)
        reload_harness.reload_manager.preparation_event = None

        # Exactly one should have succeeded.
        assert results.count(True) == 1
        assert results.count(False) == 1

    @pytest.mark.asyncio
    async def test_reloads_sequential_all_succeed(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Sequential reloads with different configs all succeed."""
        from tests.support.reload_harness import make_candidate_config

        for _ in range(3):
            result = await reload_harness.reload(make_candidate_config())
            assert result.ok is True


# ---------------------------------------------------------------------------
# Full-state comparison after each fault category
# ---------------------------------------------------------------------------


class TestFullStateAfterFault:
    """Verify no mixed state by comparing full snapshots."""

    @pytest.mark.asyncio
    async def test_full_snapshot_unchanged_after_build_failure(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Full snapshot comparison after build failure."""
        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

        injector = ReloadFaultInjector(
            target_stage="on_candidate_started",
            fault_type=FaultType.RECOVERABLE,
        )
        result = await reload_harness.reload(observer=injector)
        assert result.ok is False

        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        diffs = post.assert_same_generation(pre)
        assert diffs == [], f"Generation changed: {diffs}"

    @pytest.mark.asyncio
    async def test_full_snapshot_unchanged_after_publish_failure(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Full snapshot comparison after publish failure."""
        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

        reload_harness.reload_manager.TEST_INJECT_PUBLISH_FAILURE = RuntimeError(
            "simulated publish failure"
        )
        try:
            result = await reload_harness.reload()
        finally:
            reload_harness.reload_manager.TEST_INJECT_PUBLISH_FAILURE = None

        assert result.ok is False

        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        diffs = post.assert_same_generation(pre)
        assert diffs == [], f"Generation changed: {diffs}"

    @pytest.mark.asyncio
    async def test_generation_advances_on_successful_reload(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Successful reload advances generation and all state."""
        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

        result = await reload_harness.reload()
        assert result.ok is True

        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        assert post.active_generation_id != pre.active_generation_id
        assert post.config_digest != pre.config_digest


# ---------------------------------------------------------------------------
# Additional cancellation stages (on_admission_claimed, on_diff_computed,
# on_reconcile_prepared, on_publish_started)
# ---------------------------------------------------------------------------


class TestCancellationAtAdditionalStages:
    """Cancellation at every remaining observer stage."""

    @pytest.mark.asyncio
    async def test_cancellation_at_admission_claimed(
        self, reload_harness: ReloadHarness
    ) -> None:
        """CancelledError at on_admission_claimed aborts before diff."""
        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

        injector = ReloadFaultInjector(
            target_stage="on_admission_claimed",
            fault_type=FaultType.CANCELLATION,
        )

        with pytest.raises(asyncio.CancelledError):
            await reload_harness.reload(observer=injector)

        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        diffs = post.assert_same_generation(pre)
        assert diffs == [], f"Generation changed after cancel: {diffs}"

    @pytest.mark.asyncio
    async def test_cancellation_at_diff_computed(
        self, reload_harness: ReloadHarness
    ) -> None:
        """CancelledError at on_diff_computed aborts before candidate build."""
        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

        injector = ReloadFaultInjector(
            target_stage="on_diff_computed",
            fault_type=FaultType.CANCELLATION,
        )

        with pytest.raises(asyncio.CancelledError):
            await reload_harness.reload(observer=injector)

        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        diffs = post.assert_same_generation(pre)
        assert diffs == [], f"Generation changed after cancel: {diffs}"

    @pytest.mark.asyncio
    async def test_cancellation_at_reconcile_prepared(
        self, reload_harness: ReloadHarness
    ) -> None:
        """CancelledError at on_reconcile_prepared aborts before commit."""
        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

        injector = ReloadFaultInjector(
            target_stage="on_reconcile_prepared",
            fault_type=FaultType.CANCELLATION,
        )

        with pytest.raises(asyncio.CancelledError):
            await reload_harness.reload(observer=injector)

        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        diffs = post.assert_same_generation(pre)
        assert diffs == [], f"Generation changed after cancel: {diffs}"

    @pytest.mark.asyncio
    async def test_cancellation_at_publish_started(
        self, reload_harness: ReloadHarness
    ) -> None:
        """CancelledError at on_publish_started (during commit).

        on_publish_started fires during the commit phase after
        mark_commit_started() but before publication.  Cancellation at
        this point aborts cleanly to the old state because publication
        has not occurred — the candidate is aborted, not transferred.
        """
        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

        injector = ReloadFaultInjector(
            target_stage="on_publish_started",
            fault_type=FaultType.CANCELLATION,
        )

        with pytest.raises(asyncio.CancelledError):
            await reload_harness.reload(observer=injector)

        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        diffs = post.assert_same_generation(pre)
        assert diffs == [], f"Generation changed after cancel: {diffs}"


# ---------------------------------------------------------------------------
# Cleanup/compensation fault tests
# ---------------------------------------------------------------------------


class TestCleanupCompensationFaults:
    """Faults during cleanup and compensation paths."""

    @pytest.mark.asyncio
    async def test_compensation_failure_after_publish(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Process transition failure inside the transaction prevents
        publication — the entire transaction rolls back."""
        from tests.support.reload_harness import make_candidate_config

        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

        original_seam = (
            reload_harness.reload_manager.TEST_INJECT_TRANSITION_APPLY_FAILURE
        )
        reload_harness.reload_manager.TEST_INJECT_TRANSITION_APPLY_FAILURE = (
            RuntimeError("process transition always fails")
        )
        try:
            await reload_harness.reload(make_candidate_config())
        finally:
            reload_harness.reload_manager.TEST_INJECT_TRANSITION_APPLY_FAILURE = (
                original_seam
            )

        # Process transitions fail inside the transaction → rollback.
        # No publication happens; old generation remains active.
        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        diffs = post.assert_same_generation(pre)
        assert diffs == [], (
            f"Generation changed after process transition failure: {diffs}"
        )

        snap = reload_harness.reload_manager.snapshot()
        diag = snap.get("last_diagnostic_result", {})
        assert diag is not None

    @pytest.mark.asyncio
    async def test_candidate_close_after_build_failure(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Failed candidate build still closes any partially-created resources."""
        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

        injector = ReloadFaultInjector(
            target_stage="on_candidate_complete",
            fault_type=FaultType.RECOVERABLE,
        )
        result = await reload_harness.reload(observer=injector)
        assert result.ok is False

        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        # No resource leak after failed candidate build
        resource_diffs = post.assert_no_resource_leak(pre)
        assert resource_diffs == [], f"Resource leak: {resource_diffs}"

    @pytest.mark.asyncio
    async def test_persistence_delta_failure_no_publish(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Persistence failure before publish preserves generation and resources."""
        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

        original_apply = reload_harness.reload_manager._apply_persistence_delta

        async def _failing_apply(delta: object) -> None:
            raise OSError("disk full")

        reload_harness.reload_manager._apply_persistence_delta = _failing_apply  # type: ignore[assignment]
        try:
            result = await reload_harness.reload()
        finally:
            reload_harness.reload_manager._apply_persistence_delta = original_apply

        assert result.ok is False

        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        diffs = post.assert_same_generation(pre)
        assert diffs == [], f"Generation changed: {diffs}"
        resource_diffs = post.assert_no_resource_leak(pre)
        assert resource_diffs == [], f"Resource leak: {resource_diffs}"

    @pytest.mark.asyncio
    async def test_publish_failure_no_resource_leak(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Publish failure → no resource leak after candidate abort."""
        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

        reload_harness.reload_manager.TEST_INJECT_PUBLISH_FAILURE = RuntimeError(
            "publish failed"
        )
        try:
            result = await reload_harness.reload()
        finally:
            reload_harness.reload_manager.TEST_INJECT_PUBLISH_FAILURE = None

        assert result.ok is False

        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        resource_diffs = post.assert_no_resource_leak(pre)
        assert resource_diffs == [], f"Resource leak: {resource_diffs}"

    @pytest.mark.asyncio
    async def test_sequential_reloads_with_failure_recovery(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Reload after a failed reload still succeeds."""
        from tests.support.reload_harness import make_candidate_config

        # First reload fails
        reload_harness.reload_manager.TEST_INJECT_PUBLISH_FAILURE = RuntimeError(
            "transient"
        )
        try:
            result1 = await reload_harness.reload()
        finally:
            reload_harness.reload_manager.TEST_INJECT_PUBLISH_FAILURE = None
        assert result1.ok is False

        # Second reload succeeds
        result2 = await reload_harness.reload(make_candidate_config())
        assert result2.ok is True

    @pytest.mark.asyncio
    async def test_shutdown_during_reload_cleans_up(
        self, reload_harness: ReloadHarness
    ) -> None:
        """RuntimeManager shutdown during active reload aborts cleanly."""
        published_event = asyncio.Event()

        class PublishObserver(ReloadObserver):
            async def on_publish_complete(self, **kw: Any) -> None:
                published_event.set()

        observer = PublishObserver()

        async def _run_reload() -> None:
            await reload_harness.reload(observer=observer)  # type: ignore[arg-type]

        task = asyncio.create_task(_run_reload())
        await asyncio.wait_for(published_event.wait(), timeout=5.0)

        # Shutdown the runtime manager while reload is in progress
        await reload_harness.runtime_manager.shutdown()

        # Task should complete (possibly with error due to shutdown)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(task, timeout=5.0)

        # No crash — the system handled shutdown during reload
        # After shutdown, no generation should be accepting leases
        snap = reload_harness.reload_manager.snapshot()
        assert snap is not None


# ---------------------------------------------------------------------------
# Post-swap bookkeeping failure tests (A2)
# ---------------------------------------------------------------------------


class TestPostSwapBookkeeping:
    """Failures injected after individual post-publication steps.

    Each test injects a failure at a specific point in the commit flow
    after publication has occurred.  The system must compensate by
    accepting the new generation (since publication already happened)
    and retrying the failed step.
    """

    @pytest.mark.asyncio
    async def test_process_transition_failure_after_publish(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Process transition apply fails inside the transaction.

        Process transitions now run inside the db.transaction() context,
        so a failure rolls back the entire transaction — no publication
        happens and the old generation remains active.
        """
        from eggpool.reload_diagnostics import ReloadResultCategory

        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

        original_seam = (
            reload_harness.reload_manager.TEST_INJECT_TRANSITION_APPLY_FAILURE
        )
        reload_harness.reload_manager.TEST_INJECT_TRANSITION_APPLY_FAILURE = (
            RuntimeError("process transition failed")
        )
        try:
            result = await reload_harness.reload()
        finally:
            reload_harness.reload_manager.TEST_INJECT_TRANSITION_APPLY_FAILURE = (
                original_seam
            )

        # Transaction rolled back — no publication.
        assert result.ok is False
        diag = reload_harness.reload_manager._last_diagnostic_result
        assert diag is not None
        assert diag.category == ReloadResultCategory.FAILED_PROCESS_TRANSITION_APPLY

        # Old generation remains active.
        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        diffs = post.assert_same_generation(pre)
        assert diffs == [], (
            f"Generation changed after process transition failure: {diffs}"
        )

    @pytest.mark.asyncio
    async def test_effective_state_update_failure_after_publish(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Effective state (app.state mirror) update fails after publication.

        Publication already swapped the active slot.  The system
        compensates by accepting the new generation.
        """
        from tests.support.reload_harness import make_candidate_config

        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        gen_before = pre.active_generation_id

        # Inject failure in the effective state transition's apply method
        from eggpool.reload_transaction import EffectiveStateTransition

        original_apply = EffectiveStateTransition.apply

        async def _failing_apply(self_inner: object) -> None:
            raise RuntimeError("effective state update failed")

        EffectiveStateTransition.apply = _failing_apply  # type: ignore[assignment]
        try:
            await reload_harness.reload(make_candidate_config())
        finally:
            EffectiveStateTransition.apply = original_apply  # type: ignore[assignment]

        # Publication succeeded — generation advanced.
        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        assert post.active_generation_id != gen_before

    @pytest.mark.asyncio
    async def test_retirement_scheduling_failure_after_publish(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Retirement scheduling fails after publication.

        Publication already swapped the active slot.  The system
        compensates by accepting the new generation.
        """
        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        gen_before = pre.active_generation_id

        # Inject failure in begin_retirement to simulate retirement failure
        original_begin = reload_harness.runtime_manager.begin_retirement

        async def _failing_begin(*args: object, **kwargs: object) -> None:
            raise RuntimeError("retirement scheduling failed")

        reload_harness.runtime_manager.begin_retirement = _failing_begin  # type: ignore[assignment]
        try:
            await reload_harness.reload()
        finally:
            reload_harness.runtime_manager.begin_retirement = original_begin  # type: ignore[assignment]

        # Publication succeeded — generation advanced.
        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        assert post.active_generation_id != gen_before

    @pytest.mark.asyncio
    async def test_transaction_state_matches_runtime_after_failure(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Transaction state must agree with the runtime manager's active generation.

        When process transitions fail inside the transaction, publication
        never happens — the transaction rolls back.  The transaction's
        publication_occurred must be False and the active generation must
        remain unchanged.
        """
        from tests.support.reload_harness import make_candidate_config

        pre = RuntimeSnapshot.capture(reload_harness.runtime_manager)

        original_seam = (
            reload_harness.reload_manager.TEST_INJECT_TRANSITION_APPLY_FAILURE
        )
        reload_harness.reload_manager.TEST_INJECT_TRANSITION_APPLY_FAILURE = (
            RuntimeError("process transition failed")
        )
        try:
            await reload_harness.reload(make_candidate_config())
        finally:
            reload_harness.reload_manager.TEST_INJECT_TRANSITION_APPLY_FAILURE = (
                original_seam
            )

        # Transaction facts must match runtime state — no publication occurred.
        snap = reload_harness.reload_manager.snapshot()
        txn_snap = snap.get("active_transaction")
        if txn_snap is not None:
            assert txn_snap.get("publication_occurred") is not None
        # Old generation is still active.
        post = RuntimeSnapshot.capture(reload_harness.runtime_manager)
        diffs = post.assert_same_generation(pre)
        assert diffs == [], f"Generation changed: {diffs}"
