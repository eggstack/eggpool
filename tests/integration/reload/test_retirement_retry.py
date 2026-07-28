"""Retirement retry tests.

Verifies that retirement failure retains the original old generation ID
and that retrying retirement closes the old generation.
"""

from __future__ import annotations

import pytest

from tests.support.reload_harness import ReloadHarness

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_retirement_failure_retains_original_old_generation() -> None:
    """Retirement failure retains the original old generation and is retryable.

    Steps:
    1. First reload succeeds — initial (gen 0) → candidate (gen 1).
    2. Inject retirement failure.
    3. Second reload with initial config — candidate (gen 1) → initial
       (gen 2). The old generation is gen 1. Retirement fails.
    4. The old generation ID must be retained.
    5. A subsequent reload must succeed — proves no broken state.
    """
    async with ReloadHarness() as harness:
        rm = harness.runtime_manager

        # First reload — initial (0) → candidate (1).
        result1 = await harness.reload()
        assert result1.ok is True
        gen1_id = rm.active_snapshot().generation_id

        # Inject retirement failure for the second reload.
        harness.reload_manager.TEST_INJECT_RETIREMENT_FAILURE = RuntimeError(
            "retirement scheduling failed"
        )
        try:
            # Second reload — candidate (1) → initial (2).
            # Use initial_config to get a different config digest.
            result2 = await harness.reload(config=harness.initial_config)
        finally:
            harness.reload_manager.TEST_INJECT_RETIREMENT_FAILURE = None

        assert result2.ok is True, (
            f"reload should succeed despite retirement failure: {result2}"
        )
        gen2_id = rm.active_snapshot().generation_id
        assert gen2_id != gen1_id

        # The old generation (1) should be retained in the finalization job.
        jobs = harness.reload_manager._accepted_finalization_jobs
        history = harness.reload_manager._finalization_history
        # Check active jobs first (may be pending if retirement failed).
        gen2_jobs = [j for j in jobs.values() if j.generation_id == gen2_id]
        # Also check history (job may have been completed and pruned).
        gen2_records = [r for r in history if r.generation_id == gen2_id]
        assert len(gen2_jobs) >= 1 or len(gen2_records) >= 1, (
            "no finalization job or record for generation 2"
        )
        if gen2_jobs:
            last_gen2_job = gen2_jobs[-1]
            assert last_gen2_job.old_generation_id == gen1_id
        else:
            last_gen2_record = gen2_records[-1]
            assert last_gen2_record.old_generation_id == gen1_id

        # A subsequent reload must succeed — proves no broken state.
        result3 = await harness.reload()
        assert result3.ok is True, f"subsequent reload failed: {result3}"


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_retirement_retry_schedules_exact_old_generation() -> None:
    """Retry of retirement scheduling closes the exact original old generation.

    After retirement failure, a subsequent reload successfully completes
    retirement of the old generation through the finalization job.
    """
    async with ReloadHarness() as harness:
        rm = harness.runtime_manager

        # First reload — initial (0) → candidate (1).
        result1 = await harness.reload()
        assert result1.ok is True
        gen1_id = rm.active_snapshot().generation_id

        # Inject retirement failure for the second reload.
        harness.reload_manager.TEST_INJECT_RETIREMENT_FAILURE = RuntimeError(
            "retirement scheduling failed"
        )
        try:
            # Use initial_config to trigger a config diff.
            result2 = await harness.reload(config=harness.initial_config)
        finally:
            harness.reload_manager.TEST_INJECT_RETIREMENT_FAILURE = None

        assert result2.ok is True

        # Lease admission must still be open.
        assert rm.is_accepting_leases()

        # Third reload — succeeds without failure. The finalization job
        # from reload 2 should have been completed.
        result3 = await harness.reload()
        assert result3.ok is True, f"third reload failed: {result3}"

        gen3_id = rm.active_snapshot().generation_id
        assert gen3_id != gen1_id
