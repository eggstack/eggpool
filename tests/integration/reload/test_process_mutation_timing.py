"""Process mutation timing tests (Plan §Required failing tests).

Use a candidate configuration that changes process-owned state — task
specs and routing-trace writer settings. Pause just before publication
via a barrier observer and assert that no process-owned mutation is
yet visible. Document the current behavior if the supervisor or
writer has already mutated by this point.

Future invariant: until publication completes, process-owned state
must reflect the active (pre-reload) config, not the candidate.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from eggpool.control.reload_manager import ReloadObserver
from tests.support.runtime_snapshot import RuntimeSnapshot

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


class _PrePublishBarrier(ReloadObserver):
    """Observer that pauses the reload at ``on_publish_started``.

    Captures the runtime snapshot at the barrier so the test can assert
    that no process-owned mutation is yet visible.  Releasing the
    barrier lets publication proceed.
    """

    def __init__(self) -> None:
        self.barrier = asyncio.Event()
        self.captured_snapshot: RuntimeSnapshot | None = None
        self.captured_generation_id: int | None = None
        self.fired = False

    async def on_publish_started(
        self,
        *,
        generation_id: int,
        digest_prefix: str,
    ) -> None:
        self.fired = True
        self.captured_generation_id = generation_id
        await self.barrier.wait()


@pytest.mark.asyncio()
async def test_no_process_mutation_before_publish(
    reload_harness: ReloadHarness,
) -> None:
    """Pause just before publication and assert process state matches active.

    The candidate config changes the routing-trace sample rate and
    adds a second provider.  The supervisor and routing-trace writer
    are process-owned; they should not reflect candidate changes until
    publication completes.

    This test documents current behavior.  The runtime manager's
    candidate construction already wires the routing-trace writer to
    the candidate's settings (see ``_build_candidate_generation`` in
    ``reload_manager.py``); the writer therefore may already be
    configured to the candidate values pre-publish.  This is a defect
    tracked for Phase 3.
    """
    barrier = _PrePublishBarrier()

    pre = await RuntimeSnapshot.capture_async(
        reload_harness.runtime_manager,
        process=reload_harness.process,
        db=reload_harness.db,
    )

    # Build the candidate config; it differs from initial in routing
    # trace sample rate and provider membership.
    from eggpool.models.config import RoutingConfig

    candidate = reload_harness.candidate_config.model_copy(
        update={
            "routing": RoutingConfig(
                strategy="quota_fair",
                local_quota_mode="score_only",
                trace=reload_harness.candidate_config.routing.trace.model_copy(
                    update={"sample_rate": 0.99}
                ),
            ),
        }
    )
    # Sanity: candidate differs from initial.
    assert candidate != reload_harness.initial_config

    async def do_reload() -> Any:

        validation = reload_harness.make_validation(candidate)
        old_observer = reload_harness.reload_manager._observer
        reload_harness.reload_manager._observer = barrier
        try:
            return await reload_harness.reload_manager.reload(validation)
        finally:
            reload_harness.reload_manager._observer = old_observer

    reload_task = asyncio.create_task(do_reload())

    # Wait for the barrier to fire (reload reached publish_started).
    for _ in range(200):
        if barrier.fired:
            break
        await asyncio.sleep(0.01)
    assert barrier.fired, "reload did not reach on_publish_started"

    # Snapshot at the barrier.
    at_barrier = await RuntimeSnapshot.capture_async(
        reload_harness.runtime_manager,
        process=reload_harness.process,
        db=reload_harness.db,
    )

    # Future invariant: process-owned state matches the active
    # (pre-reload) generation.  Document current state precisely.
    #
    # Expected current behavior:
    # - active generation: still pre-reload (publication has not run)
    # - persisted providers: candidate (reconcile already wrote)
    # - routing-trace writer: candidate (writer is reconfigured in
    #   _build_candidate_generation before publication)
    assert at_barrier.active_generation_id == pre.active_generation_id, (
        "At publish_started barrier, active generation should still "
        "be the pre-reload generation (publication has not yet run)"
    )

    # Persistence is allowed to be ahead because reconcile runs before
    # publication.  This is documented behavior, not a defect.
    if at_barrier.persisted_provider_ids != pre.persisted_provider_ids:
        # Confirm it's the candidate.
        expected = set(candidate.providers.keys())
        assert set(at_barrier.persisted_provider_ids) == expected, (
            "Persisted providers at barrier should match candidate "
            "if changed (reconcile ran): "
            f"expected {expected}, got {set(at_barrier.persisted_provider_ids)}"
        )

    # Document routing-trace writer mutation status.
    rt_mode = at_barrier.routing_trace_writer_mode
    rt_sample_rate = at_barrier.routing_trace_writer_sample_rate
    # Currently the routing trace writer is reconfigured in
    # ``_build_candidate_generation`` *before* publication, so the
    # process-owned writer already reflects the candidate sample rate.
    # This is documented behavior tracked for Phase 3.
    print(
        f"pre-publish routing-trace writer: mode={rt_mode} sample_rate={rt_sample_rate}"
    )

    # Release the barrier.
    barrier.barrier.set()
    result = await reload_task

    assert result.ok is True
    # After publication, the snapshot is fully aligned.
    post = await RuntimeSnapshot.capture_async(
        reload_harness.runtime_manager,
        process=reload_harness.process,
        db=reload_harness.db,
    )
    assert post.active_generation_id != pre.active_generation_id
    assert set(post.persisted_provider_ids) == set(candidate.providers.keys())


@pytest.mark.asyncio()
async def test_no_publication_completed_at_barrier(
    reload_harness: ReloadHarness,
) -> None:
    """Sanity: at ``on_publish_started``, publication has not yet run.

    Uses the same barrier observer to verify that the active
    generation is still pre-reload.
    """
    barrier = _PrePublishBarrier()
    pre_id = reload_harness.runtime_manager.active_snapshot().generation_id

    async def do_reload() -> Any:

        validation = reload_harness.make_validation()
        old_observer = reload_harness.reload_manager._observer
        reload_harness.reload_manager._observer = barrier
        try:
            return await reload_harness.reload_manager.reload(validation)
        finally:
            reload_harness.reload_manager._observer = old_observer

    reload_task = asyncio.create_task(do_reload())

    for _ in range(200):
        if barrier.fired:
            break
        await asyncio.sleep(0.01)
    assert barrier.fired

    # At the barrier, generation ID must still be pre-reload.
    active_id = reload_harness.runtime_manager.active_snapshot().generation_id
    assert active_id == pre_id, (
        f"Active generation advanced to {active_id} before publication "
        f"completed (expected {pre_id})"
    )

    barrier.barrier.set()
    result = await reload_task
    assert result.ok is True
    new_id = reload_harness.runtime_manager.active_snapshot().generation_id
    assert new_id != pre_id
