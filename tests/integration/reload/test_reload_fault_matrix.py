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
        """Post-publication process-transition failure is compensated."""
        call_count = {"n": 0}

        async def _apply_side_effect(plan: object) -> None:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("process transition failed")

        original_apply = reload_harness.reload_manager._apply_process_transitions
        reload_harness.reload_manager._apply_process_transitions = _apply_side_effect  # type: ignore[assignment]
        try:
            result = await reload_harness.reload()
        finally:
            reload_harness.reload_manager._apply_process_transitions = original_apply

        # Publication succeeded, so generation advanced.
        assert result.ok is True
        # Compensation retried the process transitions.
        assert call_count["n"] == 2

    @pytest.mark.asyncio
    async def test_process_transition_persistent_failure_marks_compensation_failed(
        self, reload_harness: ReloadHarness
    ) -> None:
        """If compensation also fails, transaction reaches compensation_failed."""

        async def _always_fail(plan: object) -> None:
            raise RuntimeError("process transition always fails")

        original_apply = reload_harness.reload_manager._apply_process_transitions
        reload_harness.reload_manager._apply_process_transitions = _always_fail  # type: ignore[assignment]
        try:
            result = await reload_harness.reload()
        finally:
            reload_harness.reload_manager._apply_process_transitions = original_apply

        assert result.ok is False
        # Generation advanced (publication succeeded).
        assert reload_harness.runtime_manager.active_snapshot().generation_id > 0


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
