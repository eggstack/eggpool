"""Focused tests for the consolidated generation-owned terminal owner."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from eggpool.request.finalization_job import (
    AttemptRuntimeLease,
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


# ---------------------------------------------------------------------------
# Coordinator terminal-owner regressions
# ---------------------------------------------------------------------------


def _capacity_selected() -> tuple[Any, Any]:
    from eggpool.request.coordinator import SelectedAttempt
    from eggpool.request.finalizer import FinalizationData, FinalizationOutcome

    lease = AttemptRuntimeLease(
        account_name="account-1",
        estimated_tokens=100,
        estimated_microdollars=5,
        active_count_acquired=True,
        quota_reservation_acquired=True,
        health_probe_acquired=True,
    )
    selected = SelectedAttempt(
        proxy_request_id="req-cap",
        db_request_id="db-cap",
        attempt_id=7,
        reservation_id="res-cap",
        account_id=1,
        account_name="account-1",
        api_key="key",
        model_id="model-a",
        estimated_tokens=100,
        estimated_microdollars=5,
        attempt_number=1,
        provider_id="openai",
        runtime_lease=lease,
    )
    data = FinalizationData(outcome=FinalizationOutcome.COMPLETED)
    return selected, data


@pytest.mark.asyncio
async def test_capacity_rejection_after_handoff_releases_runtime_lease() -> None:
    """A post-handoff capacity rejection releases every held lease component.

    Ownership was never transferred to a finalization job, so the
    router active count, quota reservation, and health probe must all
    be released here; otherwise the account's active count and
    reserved total leak until process restart.
    """
    from eggpool.request.coordinator import RequestCoordinator

    coordinator = object.__new__(RequestCoordinator)

    class _SaturatedSupervisor:
        def register_or_get(self, *_args: Any, **_kwargs: Any) -> Any:
            raise FinalizationCapacityError()

    coordinator._finalization_supervisor = _SaturatedSupervisor()
    coordinator._router = MagicMock()
    coordinator._quota_estimator = MagicMock()
    coordinator._health_manager = MagicMock()

    context = SimpleNamespace(
        request_id="req-cap",
        protocol="openai",
        upstream_protocol="openai",
    )
    selected, data = _capacity_selected()
    data.downstream_started = True

    await coordinator._finalize_terminal(context, selected, data)

    coordinator._router.decrement_active_request_count.assert_called_once_with(
        "account-1"
    )
    coordinator._quota_estimator.remove_reservation.assert_called_once_with(
        "account-1", 5, requests=1, tokens=100
    )
    coordinator._health_manager.release_request.assert_called_once_with("account-1")
    assert selected.runtime_lease.released is True


@pytest.mark.asyncio
async def test_local_dispatch_error_is_finalized_as_client_error() -> None:
    """Local preparation failures persist as client errors, not upstream.

    Local dispatch failures have zero provider effects; recording them
    as ``UPSTREAM_ERROR`` inflates the account's consecutive-failure
    counter and skews per-account error-rate stats.
    """
    from eggpool.failure.effects import FailureEffects
    from eggpool.failure.observation import FailureObservation as Observation
    from eggpool.request.coordinator import (
        RequestCoordinator,
        SelectedAttempt,
        _LocalDispatchError,
    )
    from eggpool.request.finalizer import FinalizationOutcome

    observation = Observation(
        source="local_preparation",
        status_code=None,
        error_class="ValueError",
        provider_id="openai",
        account_name="account-1",
        model_id="model-a",
        upstream_model_id="model-a",
        client_protocol="openai",
        upstream_protocol="openai",
        response_signal=None,
        retry_after_s=None,
        response_started=False,
        downstream_started=False,
    )
    local_error = _LocalDispatchError(
        stage="request_preparation",
        error_class="ValueError",
        failure_observation=observation,
        failure_effects=FailureEffects(
            retry=False,
            retry_scope="none",
            client_outcome="client_error",
            account_effect="none",
            model_effect="none",
            circuit_penalty=False,
            persist_backoff=False,
            backoff_reason=None,
            backoff_until=None,
            release_probe_only=True,
            evidence_class="local_preparation_local",
            source="local_preparation",
        ),
    )

    coordinator = object.__new__(RequestCoordinator)
    coordinator._persist_error_detail = False
    captured: dict[str, Any] = {}

    async def _capture(_context: Any, _selected: Any, data: Any) -> None:
        captured["data"] = data

    coordinator._finalize_terminal = _capture  # type: ignore[method-assign]

    context = SimpleNamespace(
        request_id="req-local",
        model_id="model-a",
        protocol="openai",
        upstream_protocol="openai",
        original_body=b"{}",
        original_body_size=2,
        client_metadata={},
        transcode_required=False,
        transcode_context=None,
        response_handoff=SimpleNamespace(started=False),
        started_monotonic=0.0,
        upstream_connect_ms=None,
        upstream_headers_ms=None,
        thinking_trace=None,
        segmentation=None,
        segmentation_not_collected=False,
        streaming=False,
    )
    selected = SelectedAttempt(
        proxy_request_id="req-local",
        db_request_id="db-local",
        attempt_id=1,
        reservation_id="res-local",
        account_id=1,
        account_name="account-1",
        api_key="key",
        model_id="model-a",
        estimated_tokens=0,
        estimated_microdollars=0,
        attempt_number=1,
        provider_id="openai",
    )

    response = await coordinator._handle_exhausted(
        context=context,
        last_error=local_error,
        last_upstream_response=None,
        attempt_num=1,
        last_selected=selected,
    )

    data = captured["data"]
    assert data.outcome is FinalizationOutcome.CLIENT_ERROR
    assert data.error_class == "ValueError"
    assert response.status_code == 500
