"""Regression tests for strongly-referenced fire-and-forget tasks.

BUG-003: ``_schedule_finalization_reconciliation`` and
``_schedule_finalization_event`` used to drop their ``asyncio.Task``
references.  The event loop only keeps weak references to tasks, so a
pending one-shot task could be garbage-collected before it ran.  Both
sites must now hold tasks in ``ReloadManager._background_tasks`` until
completion.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from eggpool.control.accepted_finalization import (
    AcceptedFinalizationOutcome,
    FinalizationStatus,
)
from eggpool.control.reload_manager import ReloadManager
from eggpool.runtime_manager import RuntimeManager


def _outcome() -> AcceptedFinalizationOutcome:
    return AcceptedFinalizationOutcome(
        completed=True,
        next_step=None,
        attempt_count=1,
        failure_count=0,
        retry_attempt_count=0,
        retirement_retry_attempt_count=0,
        failed_step=None,
        error_class=None,
        error_message=None,
        retry_permitted=False,
        status=FinalizationStatus.COMPLETED,
    )


async def _wait_until_drained(manager: ReloadManager) -> None:
    for _ in range(100):
        if not manager._background_tasks:
            return
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_finalization_event_task_is_strongly_referenced() -> None:
    manager = ReloadManager(RuntimeManager(), MagicMock())
    job = SimpleNamespace(generation_id=1, transaction=None)

    manager._schedule_finalization_event("reload_committed", job, _outcome())

    assert manager._background_tasks, (
        "the scheduled event task must be held by a strong reference"
    )
    await _wait_until_drained(manager)
    assert not manager._background_tasks


@pytest.mark.asyncio
async def test_finalization_reconciliation_task_is_strongly_referenced() -> None:
    manager = ReloadManager(RuntimeManager(), MagicMock())
    job = SimpleNamespace(request_id="req-1")
    retained = asyncio.create_task(asyncio.sleep(0))

    manager._schedule_finalization_reconciliation(job, retained)

    assert manager._background_tasks, (
        "the scheduled observation task must be held by a strong reference"
    )
    await _wait_until_drained(manager)
    assert not manager._background_tasks
