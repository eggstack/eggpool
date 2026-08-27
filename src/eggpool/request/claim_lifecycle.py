"""Claim lifecycle compensation helpers extracted from RequestCoordinator."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eggpool.request.attempt_finalizer import AttemptFinalizer
    from eggpool.request.finalization_job import (
        ClaimCompensationProgress,
        ClaimCompensationSubmission,
    )

logger = logging.getLogger(__name__)


async def run_claim_compensation(
    *,
    submission: ClaimCompensationSubmission,
    progress: ClaimCompensationProgress,
    quota_estimator: Any | None,  # noqa: ANN401
    router: Any,  # noqa: ANN401
    attempt_finalizer: AttemptFinalizer,
    health_manager: Any | None,  # noqa: ANN401
) -> None:
    """Release a committed claim one acquired component at a time."""
    receipt = submission.receipt

    if receipt.pending_request_added and not progress.pending_load_released:
        if receipt.pending_load_converted or receipt.pending_load_released:
            progress.pending_load_released = True
        elif quota_estimator is None:
            raise RuntimeError("pending claim compensation requires quota estimator")
        else:
            quota_estimator.release_pending_claim(
                submission.account_name,
                tokens=submission.estimated_tokens,
                cost=submission.estimated_microdollars,
            )
            receipt.pending_load_released = True
            progress.pending_load_released = True
    else:
        progress.pending_load_released = True

    if receipt.active_count_added and not progress.active_count_released:
        await router.decrement_active_request_count(submission.account_name)
        progress.active_count_released = True
    elif not receipt.active_count_added:
        progress.active_count_released = True

    if (
        receipt.quota_reservation_added
        and not progress.quota_reservation_released
        and quota_estimator is not None
    ):
        await quota_estimator.remove_reservation(
            submission.account_name,
            submission.estimated_microdollars,
            requests=1,
            tokens=submission.estimated_tokens,
        )
        progress.quota_reservation_released = True
    elif not receipt.quota_reservation_added or quota_estimator is None:
        progress.quota_reservation_released = True

    if (
        not progress.durable_attempt_finalized
        or not progress.durable_reservation_converged
    ):
        if (
            submission.identity.attempt_id is not None
            and submission.identity.reservation_id is not None
        ):
            from eggpool.request.attempt_finalizer import AttemptFinalizationData
            from eggpool.retry.classification import RetryCategory

            result = await attempt_finalizer.finalize_failed_attempt(
                attempt_id=submission.identity.attempt_id,
                reservation_id=submission.identity.reservation_id,
                data=AttemptFinalizationData(
                    request_id=submission.identity.db_request_id,
                    status_code=None,
                    error_class="PostCommitInterrupted",
                    release_reason="post_commit_interrupted",
                    retry_category=RetryCategory.NEVER.value,
                    bytes_received=submission.bytes_received,
                    latency_ms=submission.latency_ms,
                    is_retry_outcome=False,
                ),
            )
            progress.durable_reservation_converged = result.reservation_converged
        else:
            progress.durable_reservation_converged = True
        progress.durable_attempt_finalized = True

    if not progress.probe_released:
        if (
            receipt.health_probe_acquired
            and not receipt.health_probe_released
            and health_manager is not None
        ):
            health_manager.release_request(submission.account_name)
            receipt.health_probe_released = True
        progress.probe_released = True

    progress.completed = all(
        (
            progress.active_count_released,
            progress.quota_reservation_released,
            progress.pending_load_released,
            progress.durable_attempt_finalized,
            progress.durable_reservation_converged,
            progress.probe_released,
        )
    )


def release_unpublished_claim(
    *,
    account_name: str,
    estimated_tokens: int,
    estimated_microdollars: int = 0,
    receipt: Any,  # RuntimePublicationReceipt  # noqa: ANN401
    quota_estimator: Any | None,  # noqa: ANN401
    health_manager: Any | None,  # noqa: ANN401
) -> None:
    """Release provisional claim ownership before durable publication.

    This is intentionally synchronous and database-free.  It is used
    for persistence, cancellation, and identity failures while the
    durable claim has no retained finalization identity yet.
    """
    if (
        receipt.pending_request_added
        and not receipt.pending_load_converted
        and not receipt.pending_load_released
    ):
        if quota_estimator is None:
            raise RuntimeError("pending claim release requires the quota estimator")
        release_pending = getattr(quota_estimator, "release_pending_claim", None)
        if not callable(release_pending):
            raise RuntimeError("quota estimator cannot release pending claims")
        release_pending(
            account_name,
            tokens=estimated_tokens,
            cost=estimated_microdollars,
        )
        receipt.pending_load_released = True

    if receipt.health_probe_acquired and not receipt.health_probe_released:
        if health_manager is None:
            raise RuntimeError("health probe release requires the health manager")
        health_manager.release_request(account_name)
        receipt.health_probe_released = True
