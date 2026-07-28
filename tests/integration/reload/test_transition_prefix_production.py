"""Production-boundary A/B/C transition-prefix proof."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from eggpool.reload_transaction import ProcessTransition, ProcessTransitionPlan

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


class _TraceTransition(ProcessTransition):
    def __init__(
        self,
        name: str,
        trace: list[str],
        *,
        fail_apply: bool = False,
    ) -> None:
        super().__init__(name, name)
        self._trace = trace
        self._fail_apply = fail_apply

    async def apply(self) -> None:
        self._trace.append(f"{self.name}.apply")
        if self._fail_apply:
            raise RuntimeError(f"{self.name} failed")

    async def rollback(self) -> None:
        self._trace.append(f"{self.name}.rollback")


@pytest.mark.asyncio()
@pytest.mark.integration()
@pytest.mark.reload()
async def test_transition_prefix_rollback_uses_real_reload_path(
    reload_harness: ReloadHarness,
) -> None:
    """A/B/C applies through ReloadManager and rolls back only A."""
    rm = reload_harness.reload_manager
    rtm = reload_harness.runtime_manager
    trace: list[str] = []
    plan = ProcessTransitionPlan(
        task_specs=(),
        callback_factories={},
        transitions=(
            _TraceTransition("A", trace),
            _TraceTransition("B", trace, fail_apply=True),
            _TraceTransition("C", trace),
        ),
    )
    rm.TEST_INJECT_PROCESS_TRANSITION_PLAN = plan
    generation_before = rtm.active_snapshot().generation_id
    provider_count_before = await reload_harness.db.fetch_one(
        "SELECT COUNT(*) AS count FROM providers"
    )
    assert provider_count_before is not None
    try:
        result = await reload_harness.reload()
    finally:
        rm.TEST_INJECT_PROCESS_TRANSITION_PLAN = None

    assert result.ok is False
    assert trace == ["A.apply", "B.apply", "A.rollback"]
    assert rtm.active_snapshot().generation_id == generation_before
    assert rtm.is_accepting_leases()
    assert rtm.diagnostics().pending_swap_state is None
    assert rm.snapshot()["unresolved_finalization_count"] == 0
    cleanup = rm.snapshot()["last_cleanup_diagnostics"]
    assert cleanup is not None
    assert cleanup["generation_id"] != generation_before
    assert len(cleanup["resource_types_closed"]) == len(
        set(cleanup["resource_types_closed"])
    )

    # The persistence delta was inside the rolled-back SQLite transaction.
    provider_count = await reload_harness.db.fetch_one(
        "SELECT COUNT(*) AS count FROM providers"
    )
    assert provider_count is not None
    assert int(provider_count["count"]) == int(provider_count_before["count"])

    subsequent = await reload_harness.reload()
    assert subsequent.ok is True
