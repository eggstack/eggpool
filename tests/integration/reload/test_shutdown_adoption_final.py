"""Shutdown ownership and exact close-count proofs."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from eggpool.control.reload_manager import ReloadObserver

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


class _BlockingCandidateObserver(ReloadObserver):
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def on_candidate_complete(self, **_: object) -> None:
        self.entered.set()
        await asyncio.Event().wait()


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_shutdown_adopts_persistent_retirement_failure(
    reload_harness: ReloadHarness,
) -> None:
    """Persistent retirement failure becomes explicit shutdown adoption."""
    rm = reload_harness.reload_manager
    rtm = reload_harness.runtime_manager

    await reload_harness.reload()
    old_generation_id = rtm.active_snapshot().generation_id
    rm.TEST_PERSISTENT_RETIREMENT_FAILURE = RuntimeError("still failing")
    try:
        result = await reload_harness.reload(config=reload_harness.initial_config)
        assert result.finalization_status == "retirement_schedule_failed"
        active_generation_id = rtm.active_snapshot().generation_id

        preparation = await rm.prepare_for_shutdown(
            transaction_timeout_s=1.0,
            finalization_timeout_s=0.01,
        )
        assert preparation.transaction_wait_completed is True
        assert preparation.adopted_jobs == 1
        assert preparation.ownership_safe_for_runtime_shutdown is True
        assert rm.snapshot()["accepted_finalization_jobs"] == []
        adopted = rm.snapshot()["shutdown_adopted_finalization_jobs"]
        assert len(adopted) == 1
        assert adopted[0]["status"] == "shutdown_adopted"
        assert adopted[0]["adopted_for_shutdown"] is True
        assert adopted[0]["references_released"] is False

        await rtm.shutdown()
        await rm.release_shutdown_adopted_references()
        close_counts = rtm.close_counts()
        assert close_counts[old_generation_id]["client_pool"] == 1
        assert close_counts[active_generation_id]["client_pool"] == 1

        history = [
            record
            for record in rm._finalization_history
            if record.generation_id == active_generation_id
        ]
        assert history
        assert history[-1].completion_status == "shutdown_adopted"
        assert history[-1].references_released is True
    finally:
        rm.TEST_PERSISTENT_RETIREMENT_FAILURE = None


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_shutdown_timeout_cancels_preacceptance_and_restores_ownership(
    reload_harness: ReloadHarness,
) -> None:
    """A preacceptance timeout completes cleanup before runtime shutdown."""
    rm = reload_harness.reload_manager
    rtm = reload_harness.runtime_manager
    observer = _BlockingCandidateObserver()
    old_generation_id = rtm.active_snapshot().generation_id

    reload_task = asyncio.create_task(reload_harness.reload(observer=observer))
    await asyncio.wait_for(observer.entered.wait(), timeout=10.0)

    preparation = await rm.prepare_for_shutdown(
        transaction_timeout_s=0.01,
        finalization_timeout_s=0.01,
    )
    assert preparation.transaction_wait_completed is True
    assert preparation.ownership_safe_for_runtime_shutdown is True
    assert rm.active_transaction is None
    assert rtm.active_snapshot().generation_id == old_generation_id
    assert rtm.is_accepting_leases()

    with pytest.raises(asyncio.CancelledError):
        await reload_task

    cleanup = rm.snapshot()["last_cleanup_diagnostics"]
    assert cleanup is not None
    assert cleanup["ownership_state_at_failure"] == "prepared"
    assert cleanup["resource_types_registered"]
    assert len(cleanup["resource_types_closed"]) == len(
        set(cleanup["resource_types_closed"])
    )

    await rtm.shutdown()
    close_counts = rtm.close_counts()
    assert close_counts[old_generation_id]["client_pool"] == 1
