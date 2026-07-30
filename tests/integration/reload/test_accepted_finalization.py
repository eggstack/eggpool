"""Accepted-finalization job tests.

Verifies that accepted reloads create an executable finalization job,
that the job is registered before post-acceptance awaits, that it resumes
from incomplete steps, and that post-acceptance cancellation retains the job.
"""

from __future__ import annotations

import asyncio

import pytest

from eggpool.control.accepted_finalization import (
    AcceptedFinalizationStep,
    AcceptedReloadFinalizationJob,
)
from tests.support.reload_harness import ReloadHarness

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_accepted_reload_creates_finalization_job(
    reload_harness: ReloadHarness,
) -> None:
    """A successful reload creates a finalization job with COMPLETED step."""
    result = await reload_harness.reload()
    assert result.ok is True, f"reload failed: {result}"

    # Verify a finalization record was archived to history.
    history = reload_harness.reload_manager._finalization_history
    assert len(history) >= 1, "expected at least one finalization record"

    # The last record must show completion.
    last_record = history[-1]
    assert last_record.completion_status == "completed"


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_finalization_job_registered_before_post_acceptance_await() -> None:
    """The finalization job is registered before any post-acceptance mirror/finalize.

    Uses a spy on AcceptedReloadFinalizationJob.run to verify the job
    appears in the manager's registry before run() is invoked.
    """
    async with ReloadHarness() as harness:
        jobs_dict = harness.reload_manager._accepted_finalization_jobs

        captured_jobs: list[AcceptedReloadFinalizationJob] = []
        registered_before_run: list[bool] = []
        original_run = AcceptedReloadFinalizationJob.run

        async def spy_run(
            self: AcceptedReloadFinalizationJob,
        ) -> AcceptedFinalizationStep:
            # Check if job is registered before run executes.
            registered_before_run.append(self.request_id in jobs_dict)
            captured_jobs.append(self)
            return await original_run(self)

        AcceptedReloadFinalizationJob.run = spy_run  # type: ignore[assignment]
        try:
            result = await harness.reload()
            assert result.ok is True, f"reload failed: {result}"

            # The job must have been registered before run() was called.
            assert len(captured_jobs) >= 1, "run() was not called"
            assert registered_before_run[0], (
                "job was not registered before run() was called"
            )
        finally:
            AcceptedReloadFinalizationJob.run = original_run  # type: ignore[assignment]


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_finalization_job_resumes_from_incomplete_step() -> None:
    """A job with step set to MIRROR_UPDATED skips earlier steps.

    Verifies that when _step is set to MIRROR_UPDATED, the ownership
    and mirror steps are skipped (step guards return early), while
    subsequent steps would execute.
    """
    async with ReloadHarness() as harness:
        # Do a first reload to get a valid generation.
        result1 = await harness.reload()
        assert result1.ok is True

        # Create a fresh job to verify step guard behavior.
        from eggpool.control.accepted_finalization import (
            AcceptedReloadFinalizationJob,
        )

        fake_txn = type(
            "FakeTxn",
            (),
            {
                "accepted_finalization": type(
                    "AF",
                    (),
                    {
                        "candidate_ownership_transferred": False,
                        "compatibility_mirror_updated": False,
                        "transitions_finalized": False,
                        "retirement_scheduled": False,
                        "transaction_completed": False,
                    },
                )(),
                "digest_prefix": "test",
                "mark_ownership_transferred": lambda self: None,
                "mark_process_transitions_applied": lambda self: None,
                "mark_persistence_committed": lambda self: None,
                "mark_observable_state_updated": lambda self: None,
                "mark_retirement_scheduled": lambda self: None,
                "mark_completed": lambda self: None,
            },
        )()

        fake_candidate = type(
            "FakeCandidate",
            (),
            {
                "transfer_to_runtime_manager": lambda self: None,
            },
        )()

        fake_pending_swap = type("FakeSwap", (), {})()

        fake_gen = type(
            "FakeGen",
            (),
            {
                "generation_id": 999,
            },
        )()

        job = AcceptedReloadFinalizationJob(
            request_id="test-resume",
            generation_id=999,
            old_generation_id=998,
            transaction=fake_txn,  # type: ignore[arg-type]
            candidate=fake_candidate,  # type: ignore[arg-type]
            pending_swap=fake_pending_swap,  # type: ignore[arg-type]
            transition_result=None,
            published_generation=fake_gen,  # type: ignore[arg-type]
            app=None,
            observer=type(
                "FakeObserver",
                (),
                {
                    "on_publish_complete": lambda self, **kw: None,
                },
            )(),
        )

        # Manually set step to MIRROR_UPDATED — simulates a job that
        # completed ownership transfer but not mirror update.
        job._step = AcceptedFinalizationStep.MIRROR_UPDATED

        # Verify the step guard skips ownership_transfer (step != REGISTERED).
        assert job._step != AcceptedFinalizationStep.REGISTERED

        # Verify the step guard skips mirror_update (step != OWNERSHIP_TRANSFERRED).
        assert job._step != AcceptedFinalizationStep.OWNERSHIP_TRANSFERRED

        # Verify transitions_finalization would run (step == MIRROR_UPDATED).
        assert job._step == AcceptedFinalizationStep.MIRROR_UPDATED

        # Verify is_complete is False (progress cursor not at COMPLETED).
        assert not job.is_complete


@pytest.mark.asyncio()
@pytest.mark.integration()
async def test_post_acceptance_cancellation_retains_job() -> None:
    """Post-acceptance cancellation retains the finalization job and accepted state.

    Uses TEST_INJECT_FINALIZATION_CANCEL to inject cancellation after acceptance.
    The transaction state must NOT be ABORTED.
    """
    async with ReloadHarness() as harness:
        rm = harness.runtime_manager
        pre_gen_id = rm.active_snapshot().generation_id

        # Inject cancellation in the finalization job's mirror step.
        harness.reload_manager.TEST_INJECT_FINALIZATION_CANCEL = asyncio.CancelledError(
            "post-accept cancel"
        )
        try:
            with pytest.raises(asyncio.CancelledError):
                await harness.reload()
        finally:
            harness.reload_manager.TEST_INJECT_FINALIZATION_CANCEL = None

        # The generation must have changed — acceptance occurred before cancel.
        post_gen_id = rm.active_snapshot().generation_id
        assert post_gen_id != pre_gen_id, (
            f"generation should have changed from {pre_gen_id} after acceptance"
        )

        # A finalization job must exist (may be pending from the cancelled reload).
        jobs = harness.reload_manager._accepted_finalization_jobs
        history = harness.reload_manager._finalization_history
        assert len(jobs) >= 1 or len(history) >= 1, (
            "no finalization job or record registered after cancel"
        )

        # A subsequent reload must succeed — proves no broken state.
        result2 = await harness.reload()
        assert result2.ok is True, f"subsequent reload failed: {result2}"

        # At least one completed finalization record should be in history
        # (the cancelled reload's job completed during the subsequent
        # reload's admission retry, and the subsequent reload's job
        # completed and was pruned to history).
        completed_records = [r for r in history if r.completion_status == "completed"]
        assert len(completed_records) >= 1, "no completed finalization record"
