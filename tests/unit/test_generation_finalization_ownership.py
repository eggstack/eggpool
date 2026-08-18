"""Generation retirement ownership for retained request finalization."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from eggpool.request.finalization_job import (
    FinalizationIdentity,
    RequestFinalizationSupervisor,
)
from eggpool.runtime_manager import RuntimeGeneration, RuntimeManager


def _identity(request_id: str = "request-1") -> FinalizationIdentity:
    return FinalizationIdentity(
        proxy_request_id=request_id,
        db_request_id=f"db-{request_id}",
        attempt_id=1,
        reservation_id=f"reservation-{request_id}",
        account_id=1,
        account_name="account",
        provider_id="provider",
        model_id="model",
        client_protocol="openai",
        upstream_protocol="openai",
        attempt_number=1,
    )


def _generation(
    generation_id: int,
    supervisor: RequestFinalizationSupervisor | None = None,
) -> RuntimeGeneration:
    now = time.monotonic()
    return RuntimeGeneration(
        generation_id=generation_id,
        config=MagicMock(),
        config_digest=f"digest-{generation_id}",
        registry=MagicMock(),
        catalog=MagicMock(),
        router=MagicMock(),
        coordinator=MagicMock(),
        client_pool=MagicMock(),
        outbound_manager=MagicMock(),
        health_manager=MagicMock(),
        cost_calculator=MagicMock(),
        transcoder_policy=MagicMock(),
        dispatch_overhead_recorder=MagicMock(),
        dispatch_span_recorder=MagicMock(),
        account_backoff_repo=MagicMock(),
        stats_service=MagicMock(),
        supervisor=MagicMock(),
        routing_trace_guard=MagicMock(),
        routing_trace_writer=MagicMock(),
        created_at_monotonic=now,
        created_at_epoch=now,
        finalization_supervisor=supervisor,
    )


@pytest.mark.asyncio
async def test_retiring_generation_waits_for_terminal_reference_and_resumes() -> None:
    supervisor = RequestFinalizationSupervisor(db=MagicMock())
    manager = RuntimeManager()
    await manager.install_initial(_generation(0, supervisor))

    job = supervisor.register_or_get(_identity(), "client_cancelled")
    assert manager.diagnostics().active is not None

    old_slot = manager._active  # pyright: ignore[reportPrivateUsage]
    assert old_slot is not None
    assert old_slot.terminal_references == 1

    await manager.install_candidate(_generation(1), drain_timeout_s=0.0)
    await asyncio.sleep(0)
    assert manager.diagnostics().retiring
    assert manager.diagnostics().retiring[0].terminal_references == 1
    assert manager.close_counts().get(0) is None

    await job.run()
    await asyncio.sleep(0)
    if manager._retirement_tasks:  # pyright: ignore[reportPrivateUsage]
        await asyncio.gather(
            *manager._retirement_tasks.values()  # pyright: ignore[reportPrivateUsage]
        )

    assert manager.diagnostics().retiring == ()
    assert manager.close_counts()[0]["client_pool"] == 1


@pytest.mark.asyncio
async def test_live_retirement_timeout_fail_closes_without_dependency_close() -> None:
    fatal_reasons: list[str] = []
    supervisor = RequestFinalizationSupervisor(db=MagicMock())
    manager = RuntimeManager(fatal_handler=fatal_reasons.append)
    await manager.install_initial(_generation(0, supervisor))
    supervisor.register_or_get(_identity(), "client_cancelled")

    slot = manager._active  # pyright: ignore[reportPrivateUsage]
    assert slot is not None
    await manager.begin_retirement(slot, drain_timeout_s=0.0)

    assert fatal_reasons
    assert slot.blocked_on_terminal_convergence is True
    assert manager.close_counts().get(0) is None
    assert manager.diagnostics().retiring[0].retirement_complete is False


@pytest.mark.asyncio
async def test_generation_metrics_report_bounded_ownership_facts() -> None:
    supervisor = RequestFinalizationSupervisor(db=MagicMock())
    manager = RuntimeManager()
    await manager.install_initial(_generation(7, supervisor))
    supervisor.register_or_get(_identity(), "client_cancelled")

    await manager.install_candidate(_generation(8), drain_timeout_s=0.0)
    await asyncio.sleep(0)
    snapshot = manager.finalization_ownership_snapshot()

    assert snapshot["active_generation_id"] == 8
    assert snapshot["active_supervisor"] is None
    assert snapshot["retiring_generations"] == 1
    assert snapshot["retiring_terminal_references"] == 1
    assert snapshot["blocked_on_terminal_convergence"] is False
