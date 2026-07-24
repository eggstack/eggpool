"""Plan 021 structural and state-machine closure tests."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from eggpool.control.accepted_finalization import (
    AcceptedFinalizationStep,
    AcceptedReloadFinalizationJob,
    FinalizationStatus,
)


def test_acceptance_marker_is_outside_rollback_capable_handlers() -> None:
    """Acceptance cannot be lexically governed by precommit cleanup."""
    source_path = (
        Path(__file__).parents[2] / "src" / "eggpool" / "control" / "reload_manager.py"
    )
    tree = ast.parse(source_path.read_text())
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_execute_accepted_phase"
    )
    calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    accepted = next(node for node in calls if node.func.attr == "mark_accepted")
    accounting = next(
        node for node in calls if node.func.attr == "_record_reload_accepted_once"
    )
    registration = next(
        node for node in calls if node.func.attr == "_ensure_accepted_owner_registered"
    )
    first_await = min(
        node.lineno for node in ast.walk(function) if isinstance(node, ast.Await)
    )

    assert accepted.lineno < first_await
    assert accounting.lineno < first_await
    assert registration.lineno < first_await
    for parent in _ancestors(function, accepted):
        if isinstance(parent, ast.Try):
            handler_text = ast.unparse(parent.handlers)
            assert "_abort_precommit_reload" not in handler_text
            assert "rollback_applied" not in handler_text
            assert "candidate.abort" not in handler_text


def _ancestors(root: ast.AST, target: ast.AST) -> list[ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}

    for parent in ast.walk(root):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    result: list[ast.AST] = []
    child = target
    while child in parents:
        parent = parents[child]
        result.append(parent)
        child = parent
    return result


@pytest.mark.asyncio()
async def test_retirement_failures_use_monotonic_retry_deltas() -> None:
    """Retirement-specific progress and counters are exact across retries."""
    transaction = MagicMock()
    transaction.accepted_finalization = MagicMock()
    transaction.digest_prefix = "digest"
    pending_swap = MagicMock()
    pending_swap.finalize_retirement = AsyncMock(
        side_effect=[RuntimeError("one"), RuntimeError("two"), None]
    )
    manager = MagicMock()
    manager.TEST_PERSISTENT_RETIREMENT_FAILURE = None
    manager.TEST_INJECT_RETIREMENT_FAILURE = None
    job = AcceptedReloadFinalizationJob(
        request_id="plan-021-retirement",
        generation_id=2,
        old_generation_id=1,
        transaction=transaction,
        candidate=MagicMock(),
        pending_swap=pending_swap,
        transition_result=None,
        published_generation=MagicMock(),
        app=None,
        observer=MagicMock(
            on_publish_complete=AsyncMock(),
            on_retirement_started=AsyncMock(),
        ),
        _reload_manager=manager,
        _step=AcceptedFinalizationStep.OBSERVER_REPORTED,
    )

    first = await job.run()
    second = await job.run()
    third = await job.run()

    assert first.status is FinalizationStatus.RETIREMENT_SCHEDULE_FAILED
    assert first.next_step == "retirement_scheduling"
    assert first.failure_count == 1
    assert first.retry_attempt_count == 0
    assert first.retirement_retry_attempt_count == 0
    assert second.failure_count == 2
    assert second.retry_attempt_count == 1
    assert second.retirement_retry_attempt_count == 1
    assert third.completed is True
    assert third.retry_attempt_count == 2
    assert third.retirement_retry_attempt_count == 2
    assert pending_swap.finalize_retirement.await_count == 3


@pytest.mark.asyncio()
async def test_shutdown_adoption_does_not_release_references() -> None:
    """Adoption and operational-reference release are separate facts."""
    transaction = MagicMock()
    transaction.accepted_finalization = MagicMock()
    transaction.digest_prefix = "digest"
    job = AcceptedReloadFinalizationJob(
        request_id="plan-021-adoption",
        generation_id=2,
        old_generation_id=1,
        transaction=transaction,
        candidate=MagicMock(),
        pending_swap=MagicMock(),
        transition_result=None,
        published_generation=MagicMock(),
        app=None,
        observer=MagicMock(),
    )

    await job.adopt_for_shutdown()
    assert job.adopted_for_shutdown is True
    assert job.references_released is False
    assert job.status is FinalizationStatus.SHUTDOWN_ADOPTED

    job.release_references()
    assert job.adopted_for_shutdown is True
    assert job.references_released is True
    assert job.candidate is None
