"""Background task supervisor with restart and backoff."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

logger = logging.getLogger(__name__)


@dataclass
class SupervisedTask:
    """A background task that restarts on failure with exponential backoff."""

    name: str
    _coro_factory: Callable[[], Coroutine[Any, Any, None]]
    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    _restart_count: int = 0
    _max_restarts: int = 10
    _base_delay: float = 1.0
    _max_delay: float = 300.0
    _last_failure: float = 0.0
    _running: bool = False

    async def start(self) -> None:
        """Start the supervised task."""
        if self._running:
            return
        self._restart_count = 0
        self._running = True
        self._task = asyncio.create_task(
            self._run_loop(),
            name=f"eggpool:{self.name}",
        )
        logger.info("Started supervised task %r", self.name)

    async def stop(self) -> None:
        """Stop the supervised task."""
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        logger.info("Stopped supervised task %r", self.name)

    async def _run_loop(self) -> None:
        """Run the task, restarting on failure with backoff."""
        try:
            while self._running:
                try:
                    await self._coro_factory()
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.exception("Supervised task %r failed", self.name)
                else:
                    if not self._running:
                        break
                    logger.warning(
                        "Supervised task %r completed unexpectedly",
                        self.name,
                    )

                self._restart_count += 1
                self._last_failure = time.time()
                if self._restart_count >= self._max_restarts:
                    logger.error(
                        "Supervised task %r exceeded max restarts, giving up",
                        self.name,
                    )
                    break

                delay = min(
                    self._base_delay * (2 ** (self._restart_count - 1)),
                    self._max_delay,
                )
                logger.info(
                    "Restarting task %r in %.1fs (restart %d/%d)",
                    self.name,
                    delay,
                    self._restart_count,
                    self._max_restarts,
                )
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    break
        finally:
            self._running = False

    @property
    def is_running(self) -> bool:
        """Check if the task is currently running."""
        return self._running and self._task is not None and not self._task.done()


class TaskSupervisor:
    """Manages multiple supervised background tasks."""

    def __init__(self) -> None:
        self._tasks: dict[str, SupervisedTask] = {}

    def register(
        self,
        name: str,
        coro_factory: Callable[[], Coroutine[Any, Any, None]],
        max_restarts: int = 10,
    ) -> SupervisedTask:
        """Register a new supervised task."""
        if name in self._tasks:
            raise ValueError(f"Task {name!r} is already registered")
        task = SupervisedTask(
            name=name,
            _coro_factory=coro_factory,
            _max_restarts=max_restarts,
        )
        self._tasks[name] = task
        return task

    async def start_all(self) -> None:
        """Start all registered tasks."""
        for task in self._tasks.values():
            await task.start()

    async def stop_all(self) -> None:
        """Stop all registered tasks."""
        for task in self._tasks.values():
            await task.stop()

    def get_task(self, name: str) -> SupervisedTask | None:
        """Get a task by name."""
        return self._tasks.get(name)

    @property
    def all_healthy(self) -> bool:
        """Check if all tasks are running."""
        if not self._tasks:
            return False
        return all(t.is_running for t in self._tasks.values())
