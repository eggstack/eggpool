"""Focused retained coordinator-cleanup convergence tests."""

from __future__ import annotations

import pytest

from eggpool.request.attempt_finalizer import AttemptFinalizeResult
from eggpool.request.coordinator import (
    AttemptCleanupProgress,
    ClaimCompensationProgress,
    ProxyRequestContext,
    RequestCoordinator,
    RuntimePublicationReceipt,
    SelectedAttempt,
    _RetryableUpstreamError,
)


def _context() -> ProxyRequestContext:
    return ProxyRequestContext(
        request_id="req-1",
        protocol="openai",
        model_id="model-1",
        streaming=False,
        original_body=b"{}",
        incoming_headers={},
    )


def _selected() -> SelectedAttempt:
    return SelectedAttempt(
        proxy_request_id="req-1",
        db_request_id="db-1",
        attempt_id=1,
        reservation_id="reservation-1",
        account_id=1,
        account_name="account-1",
        api_key="key",
        model_id="model-1",
        estimated_tokens=10,
        estimated_microdollars=2,
        attempt_number=1,
    )


@pytest.mark.asyncio
async def test_attempt_cleanup_rejoins_after_quota_failure() -> None:
    coordinator = object.__new__(RequestCoordinator)
    calls = {"finalizer": 0, "quota": 0, "active": 0, "health": 0}

    class Finalizer:
        async def finalize_failed_attempt(
            self, **kwargs: object
        ) -> AttemptFinalizeResult:
            calls["finalizer"] += 1
            return AttemptFinalizeResult(True, True)

    class Quota:
        async def remove_reservation(self, *args: object, **kwargs: object) -> None:
            calls["quota"] += 1
            if calls["quota"] == 1:
                raise RuntimeError("quota busy")

    class Router:
        async def decrement_active_request_count(self, account_name: str) -> None:
            calls["active"] += 1

    coordinator._attempt_finalizer = Finalizer()
    coordinator._quota_estimator = Quota()
    coordinator._router = Router()
    coordinator._health_manager = object()
    coordinator._apply_health_transition = (  # type: ignore[method-assign]
        lambda *args, **kwargs: _health_effect(calls)
    )

    context = _context()
    selected = _selected()
    error = _RetryableUpstreamError(
        "retry",
        status_code=503,
        error_class="TemporaryUpstreamError",
    )
    progress = AttemptCleanupProgress(
        context=context,
        selected=selected,
        error=error,
    )

    with pytest.raises(RuntimeError, match="quota busy"):
        await coordinator._run_failed_attempt_cleanup(progress)
    assert progress.durable_transition_checked
    assert not progress.quota_released
    assert calls["finalizer"] == 1

    await coordinator._run_failed_attempt_cleanup(progress)
    assert progress.completed
    assert calls == {"finalizer": 1, "quota": 2, "active": 1, "health": 1}


async def _health_effect(calls: dict[str, int]) -> None:
    calls["health"] += 1


@pytest.mark.asyncio
async def test_claim_compensation_rejoins_after_publication_release() -> None:
    coordinator = object.__new__(RequestCoordinator)
    calls = {"active": 0, "quota": 0, "finalizer": 0, "probe": 0}

    class Router:
        async def decrement_active_request_count(self, account_name: str) -> None:
            calls["active"] += 1

    class Quota:
        async def remove_reservation(self, *args: object, **kwargs: object) -> None:
            calls["quota"] += 1
            if calls["quota"] == 1:
                raise RuntimeError("quota busy")

    class Finalizer:
        async def finalize_failed_attempt(
            self, **kwargs: object
        ) -> AttemptFinalizeResult:
            calls["finalizer"] += 1
            return AttemptFinalizeResult(True, True)

    class Health:
        def release_request(self, account_name: str) -> None:
            calls["probe"] += 1

    coordinator._router = Router()
    coordinator._quota_estimator = Quota()
    coordinator._attempt_finalizer = Finalizer()
    coordinator._health_manager = Health()
    identity = RequestCoordinator._ClaimIdentity(
        account_name="account-1",
        account_id=1,
        resolved_provider_id="openai",
        api_key="key",
        estimated_microdollars=2,
    )
    progress = ClaimCompensationProgress(
        context=_context(),
        claim_identity=identity,
        attempt_id=1,
        reservation_id="reservation-1",
        estimated_tokens=10,
        receipt=RuntimePublicationReceipt(
            active_count_added=True,
            quota_reservation_added=True,
        ),
    )

    with pytest.raises(RuntimeError, match="quota busy"):
        await coordinator._run_claim_compensation(progress)
    assert progress.active_count_released
    assert not progress.quota_reservation_released

    await coordinator._run_claim_compensation(progress)
    assert progress.completed
    assert calls == {"active": 1, "quota": 2, "finalizer": 1, "probe": 1}


@pytest.mark.parametrize("cleanup_kind", ["attempt", "claim"])
@pytest.mark.asyncio
async def test_cancelled_cleanup_submits_one_request_terminal(
    cleanup_kind: str,
) -> None:
    coordinator = object.__new__(RequestCoordinator)
    coordinator._attempt_cleanup_tasks = {}
    coordinator._attempt_cleanup_progress = {}
    coordinator._claim_compensation_tasks = {}
    coordinator._claim_compensation_progress = {}
    selected = _selected()
    context = _context()
    terminal_outcomes: list[str] = []

    async def finalize_terminal(*args: object) -> None:
        data = args[2]
        terminal_outcomes.append(data.outcome.value)

    coordinator._finalize_terminal = finalize_terminal  # type: ignore[method-assign]
    if cleanup_kind == "attempt":
        coordinator._attempt_cleanup_progress[("req-1", 1)] = AttemptCleanupProgress(
            completed=True
        )
        assert await coordinator._await_cleanup_then_finalize_cancelled(
            context=context,
            selected=selected,
        )
    else:
        context.client_metadata["_post_commit_selected"] = selected
        context.client_metadata["post_commit_interrupted"] = True
        coordinator._claim_compensation_progress[("req-1", 1)] = (
            ClaimCompensationProgress(completed=True)
        )
        assert await coordinator._handle_selection_cancellation(context)
    assert terminal_outcomes == ["client_cancelled"]
    assert context.client_metadata["_cancelled_request_finalized"] is True


@pytest.mark.asyncio
async def test_retained_cleanup_capacity_fails_closed() -> None:
    coordinator = object.__new__(RequestCoordinator)
    coordinator._retained_cleanup_capacity = 1
    coordinator._retained_cleanup_capacity_rejections = 0
    coordinator._attempt_cleanup_tasks = {}
    coordinator._attempt_cleanup_progress = {
        ("existing", 1): AttemptCleanupProgress(),
    }
    coordinator._claim_compensation_tasks = {}
    coordinator._claim_compensation_progress = {}

    with pytest.raises(RuntimeError, match="capacity exhausted"):
        await coordinator._cleanup_failed_attempt(
            context=_context(),
            selected=_selected(),
            error=_RetryableUpstreamError("retry"),
        )
    assert coordinator._attempt_cleanup_tasks == {}
    assert coordinator._retained_cleanup_capacity_rejections == 1
