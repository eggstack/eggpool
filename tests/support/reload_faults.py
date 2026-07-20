from __future__ import annotations

import asyncio
import enum
from typing import Any

from eggpool.control.reload_manager import ReloadObserver


class FaultType(enum.Enum):
    """Broad class of injected fault."""

    RECOVERABLE = "recoverable"
    CANCELLATION = "cancellation"
    CLOSE_FAILURE = "close_failure"


class ReloadFaultInjector(ReloadObserver):
    """Observer that injects exceptions at a named stage.

    Set `target_stage` to the stage name where the fault should fire,
    and `fault_type` to classify the error. The injector fires exactly
    once, then becomes inert.

    Usage:
        injector = ReloadFaultInjector(
            target_stage="on_candidate_started",
            fault_type=FaultType.RECOVERABLE,
        )
        result = await harness.reload(observer=injector)
        assert not result.ok
        assert injector.fired
    """

    STAGES = (
        "on_admission_claimed",
        "on_validation_complete",
        "on_diff_computed",
        "on_candidate_started",
        "on_candidate_complete",
        "on_reconcile_started",
        "on_reconcile_prepared",
        "on_publish_started",
        "on_publish_complete",
        "on_retirement_started",
    )

    def __init__(
        self,
        target_stage: str,
        fault_type: FaultType = FaultType.RECOVERABLE,
        message: str | None = None,
    ) -> None:
        if target_stage not in self.STAGES:
            raise ValueError(
                f"Unknown stage {target_stage!r}; choose from {self.STAGES}"
            )
        self.target_stage = target_stage
        self.fault_type = fault_type
        self._message = message or f"Fault at {target_stage}"
        self._fired = False
        self._fired_generation_id: int | None = None

    @property
    def fired(self) -> bool:
        return self._fired

    @property
    def fired_generation_id(self) -> int | None:
        return self._fired_generation_id

    def _make_error(self) -> Exception:
        if self.fault_type == FaultType.CANCELLATION:
            return asyncio.CancelledError(self._message)
        if self.fault_type == FaultType.CLOSE_FAILURE:
            return OSError(self._message)
        return RuntimeError(self._message)

    async def _fire(self, generation_id: int | None = None) -> None:
        if self._fired:
            return
        self._fired = True
        self._fired_generation_id = generation_id
        raise self._make_error()

    # Override all stage methods to conditionally fire

    async def on_admission_claimed(self, **kw: Any) -> None:
        if self.target_stage == "on_admission_claimed":
            await self._fire(kw.get("generation_id"))

    async def on_validation_complete(self, **kw: Any) -> None:
        if self.target_stage == "on_validation_complete":
            await self._fire(kw.get("generation_id"))

    async def on_diff_computed(self, **kw: Any) -> None:
        if self.target_stage == "on_diff_computed":
            await self._fire(kw.get("generation_id"))

    async def on_candidate_started(self, **kw: Any) -> None:
        if self.target_stage == "on_candidate_started":
            await self._fire(kw.get("generation_id"))

    async def on_candidate_complete(self, **kw: Any) -> None:
        if self.target_stage == "on_candidate_complete":
            await self._fire(kw.get("generation_id"))

    async def on_reconcile_started(self, **kw: Any) -> None:
        if self.target_stage == "on_reconcile_started":
            await self._fire(kw.get("generation_id"))

    async def on_reconcile_prepared(self, **kw: Any) -> None:
        if self.target_stage == "on_reconcile_prepared":
            await self._fire(kw.get("generation_id"))

    async def on_publish_started(self, **kw: Any) -> None:
        if self.target_stage == "on_publish_started":
            await self._fire(kw.get("generation_id"))

    async def on_publish_complete(self, **kw: Any) -> None:
        if self.target_stage == "on_publish_complete":
            await self._fire(kw.get("generation_id"))

    async def on_retirement_started(self, **kw: Any) -> None:
        if self.target_stage == "on_retirement_started":
            await self._fire(kw.get("generation_id"))
