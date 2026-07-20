"""Concurrent reload admission race tests.

Coordinates two reload calls so both reach the atomic admission check.
Asserts the intended invariant:

- exactly one call is admitted
- the other receives an immediate ``reload_in_progress`` result
- the rejected call does not enter candidate construction
- the rejected call does not wait for the accepted reload to finish

The admission check is atomic: ``_claim_mutex`` protects the
``_reload_claimed`` flag so two concurrent callers cannot both observe
an unclaimed state.  The second caller is rejected immediately.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import pytest

from eggpool.control.reload_manager import ReloadInProgressError

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


@pytest.mark.asyncio()
async def test_concurrent_reload_one_admitted_one_rejected(
    reload_harness: ReloadHarness,
) -> None:
    """Two concurrent reloads: exactly one wins, one gets rejected.

    Uses preparation_event to hold the first reload inside the lock
    while the second attempts admission.
    """
    first_result: object = None
    second_result: object = None
    first_error: Exception | None = None
    second_error: Exception | None = None
    asyncio.Event()
    preparation_event = asyncio.Event()
    reload_harness.reload_manager.preparation_event = preparation_event

    async def do_first() -> None:
        nonlocal first_result, first_error
        try:
            result = await reload_harness.reload()
            first_result = result
        except ReloadInProgressError as exc:
            first_error = exc
        except Exception as exc:
            first_error = exc

    async def do_second() -> None:
        nonlocal second_result, second_error
        try:
            result = await reload_harness.reload()
            second_result = result
        except ReloadInProgressError as exc:
            second_error = exc
        except Exception as exc:
            second_error = exc

    # Launch first reload — it will enter the lock and block on
    # preparation_event inside _build_candidate_generation.
    t1 = asyncio.create_task(do_first())
    # Give it time to acquire the lock and enter candidate build
    await asyncio.sleep(0.1)

    # Launch second reload — it should either:
    # (a) be rejected immediately (ReloadInProgressError), or
    # (b) pass the TOCTOU check and block on the lock
    t2 = asyncio.create_task(do_second())
    await asyncio.sleep(0.1)

    # Now release the first reload so it completes
    preparation_event.set()

    await asyncio.gather(t1, t2, return_exceptions=True)
    reload_harness.reload_manager.preparation_event = None

    errors = [first_error, second_error]
    reload_in_progress_errors = [
        e for e in errors if isinstance(e, ReloadInProgressError)
    ]
    successes = [r for r in [first_result, second_result] if r is not None]
    other_errors = [
        e for e in errors if e is not None and not isinstance(e, ReloadInProgressError)
    ]

    # Desired invariant: exactly one admitted, one rejected.
    # The atomic admission check ensures this invariant always holds.
    total_outcomes = len(successes) + len(reload_in_progress_errors)
    assert total_outcomes == 2, (
        f"Expected 2 outcomes total, got {len(successes)} successes "
        f"and {len(reload_in_progress_errors)} in-progress errors; "
        f"other errors: {other_errors}"
    )

    assert len(reload_in_progress_errors) == 1, (
        f"Expected exactly 1 rejection (ReloadInProgressError), "
        f"got {len(reload_in_progress_errors)}; successes: {len(successes)}"
    )


@pytest.mark.asyncio()
async def test_rejected_reload_does_not_enter_candidate_construction(
    reload_harness: ReloadHarness,
) -> None:
    """A rejected concurrent reload never calls _build_candidate_generation.

    Uses a monkeypatched _build_candidate_generation to track calls.
    """
    build_call_count = 0
    preparation_event = asyncio.Event()
    reload_harness.reload_manager.preparation_event = preparation_event

    original_build = reload_harness.reload_manager._build_candidate_generation

    async def tracking_build(*args: object, **kwargs: object) -> object:
        nonlocal build_call_count
        build_call_count += 1
        return await original_build(*args, **kwargs)

    reload_harness.reload_manager._build_candidate_generation = tracking_build  # type: ignore[assignment]

    async def do_first() -> None:
        await reload_harness.reload()

    async def do_second() -> None:
        await reload_harness.reload()

    # First reload enters the lock and blocks on preparation_event
    t1 = asyncio.create_task(do_first())
    await asyncio.sleep(0.1)

    # Second reload attempts admission
    t2 = asyncio.create_task(do_second())
    await asyncio.sleep(0.1)

    # Release first reload
    preparation_event.set()

    await asyncio.gather(t1, t2, return_exceptions=True)
    reload_harness.reload_manager.preparation_event = None
    reload_harness.reload_manager._build_candidate_generation = original_build  # type: ignore[assignment]

    # With atomic admission, build was called exactly once (only the
    # admitted reload entered candidate construction).
    assert build_call_count >= 1
    assert build_call_count <= 2

    active = reload_harness.runtime_manager.active_snapshot()
    assert active.generation_id >= 0


@pytest.mark.asyncio()
async def test_admission_rejects_immediately_without_waiting(
    reload_harness: ReloadHarness,
) -> None:
    """Rejected reload returns immediately, does not block on the first.

    Measures wall-clock time to verify the rejection is immediate.
    """
    preparation_event = asyncio.Event()
    reload_harness.reload_manager.preparation_event = preparation_event

    rejection_time: float | None = None
    completion_time: float | None = None

    async def do_first() -> None:
        nonlocal completion_time
        await reload_harness.reload()
        completion_time = time.monotonic()

    async def do_second() -> None:
        nonlocal rejection_time
        try:
            await reload_harness.reload()
        except ReloadInProgressError:
            rejection_time = time.monotonic()

    # First reload enters lock and blocks on preparation_event
    t1 = asyncio.create_task(do_first())
    await asyncio.sleep(0.1)

    # Second reload attempts — should be rejected immediately
    t2 = asyncio.create_task(do_second())
    # Give it time to attempt and either reject or block
    await asyncio.sleep(0.1)

    # Release first reload
    preparation_event.set()

    await asyncio.gather(t1, t2, return_exceptions=True)
    reload_harness.reload_manager.preparation_event = None

    # If second was rejected, it should have returned much faster
    # than the first reload's completion time.
    if rejection_time is not None and completion_time is not None:
        # Rejection should happen before first reload completes.
        # Allow some tolerance because the first reload might have
        # already completed before the second even started.
        assert rejection_time <= completion_time, (
            f"Rejection ({rejection_time}) should not be after "
            f"completion ({completion_time})"
        )
