"""Focused tests for the consolidated generation-owned terminal owner."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import MagicMock

import pytest

from eggpool.request.finalization_job import (
    ClaimCompensationProgress,
    ClaimCompensationSubmission,
    FailedAttemptCleanupProgress,
    FailedAttemptCleanupSubmission,
    FinalizationCapacityError,
    FinalizationIdentity,
    RequestFinalizationSupervisor,
    RuntimePublicationReceipt,
    TerminalConflictError,
)


def _identity(
    *, request_id: str = "req-1", attempt_id: int | None = 1
) -> FinalizationIdentity:
    return FinalizationIdentity(
        proxy_request_id=request_id,
        db_request_id="db-1" if attempt_id is not None else None,
        attempt_id=attempt_id,
        reservation_id="reservation-1" if attempt_id is not None else None,
        account_id=1,
        account_name="account-1",
        provider_id="openai",
        model_id="model-1",
        client_protocol="openai",
        upstream_protocol="openai",
        attempt_number=1 if attempt_id is not None else None,
    )


def _cleanup_submission(*, request_id: str = "req-1") -> FailedAttemptCleanupSubmission:
    return FailedAttemptCleanupSubmission(
        identity=_identity(request_id=request_id),
        status_code=503,
        error_class="TemporaryUpstreamError",
        retry_category="temporary",
        bytes_received=10,
        latency_ms=2,
    )


def _claim_submission() -> ClaimCompensationSubmission:
    return ClaimCompensationSubmission(
        identity=_identity(attempt_id=None),
        account_name="account-1",
        estimated_tokens=10,
        estimated_microdollars=2,
        bytes_received=10,
        latency_ms=2,
        receipt=RuntimePublicationReceipt(
            active_count_added=True,
            quota_reservation_added=True,
            health_probe_acquired=True,
        ),
    )


def test_mixed_command_capacity_and_generation_references() -> None:
    retained = 0
    released = 0

    def retain() -> None:
        nonlocal retained
        retained += 1

    def release() -> None:
        nonlocal released
        released += 1

    supervisor = RequestFinalizationSupervisor(
        db=MagicMock(),
        max_active_jobs=3,
        retain_generation=retain,
        release_generation=release,
    )

    async def complete(submission: object, progress: object) -> None:
        assert submission is not None
        assert isinstance(
            progress, (FailedAttemptCleanupProgress, ClaimCompensationProgress)
        )
        progress.completed = True

    selected = supervisor.register_or_get(_identity(), "client_cancelled")
    cleanup = supervisor.register_failed_attempt_cleanup(
        _cleanup_submission(), complete
    )
    claim = supervisor.register_claim_compensation(_claim_submission(), complete)
    assert supervisor.active_count == 3
    assert retained == 3

    with pytest.raises(FinalizationCapacityError):
        supervisor.register_failed_attempt_cleanup(
            _cleanup_submission(request_id="req-2"), complete
        )

    async def drain() -> int:
        await asyncio.gather(selected.run(), supervisor.run_terminal_command(cleanup))
        await supervisor.run_terminal_command(claim)
        return await supervisor.shutdown(timeout_s=1.0)

    assert asyncio.run(drain()) == 0
    assert supervisor.active_count == 0
    assert retained == released == 3
    snapshot = supervisor.snapshot()
    assert snapshot["active_by_command_kind"] == {
        "selected_request_finalization": 0,
        "failed_attempt_cleanup": 0,
        "claim_compensation": 0,
    }


def test_duplicate_commands_join_and_conflicts_fail_before_mutation() -> None:
    supervisor = RequestFinalizationSupervisor(db=MagicMock())
    calls = 0

    async def complete(submission: object, progress: object) -> None:
        nonlocal calls
        calls += 1
        assert isinstance(progress, FailedAttemptCleanupProgress)
        progress.completed = True

    first = supervisor.register_failed_attempt_cleanup(_cleanup_submission(), complete)
    duplicate = supervisor.register_failed_attempt_cleanup(
        FailedAttemptCleanupSubmission(
            identity=_identity(),
            status_code=503,
            error_class="TemporaryUpstreamError",
            retry_category="temporary",
            bytes_received=999,
            latency_ms=999,
        ),
        complete,
    )
    assert duplicate is first

    with pytest.raises(TerminalConflictError):
        supervisor.register_failed_attempt_cleanup(
            FailedAttemptCleanupSubmission(
                identity=_identity(),
                status_code=429,
                error_class="RateLimitError",
                retry_category="temporary",
                bytes_received=10,
                latency_ms=2,
            ),
            complete,
        )
    assert supervisor.active_count == 1
    asyncio.run(supervisor.run_terminal_command(first))
    assert calls == 1


@pytest.mark.asyncio
async def test_cancellation_keeps_cleanup_owned_and_progress_is_not_replayed() -> None:
    supervisor = RequestFinalizationSupervisor(db=MagicMock())
    started = asyncio.Event()
    unblock = asyncio.Event()
    calls = {"first": 0, "second": 0}

    async def cleanup(submission: object, progress: object) -> None:
        assert isinstance(progress, FailedAttemptCleanupProgress)
        calls["first"] += 1
        started.set()
        await unblock.wait()
        progress.active_count_released = True
        calls["second"] += 1
        progress.completed = True

    command = supervisor.register_failed_attempt_cleanup(_cleanup_submission(), cleanup)
    waiter = asyncio.create_task(supervisor.run_terminal_command(command))
    await started.wait()
    waiter.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await waiter
    assert not command.is_complete
    unblock.set()
    await supervisor.run_terminal_command(command)
    assert command.is_complete
    assert calls == {"first": 1, "second": 1}
