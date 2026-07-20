from __future__ import annotations

import asyncio  # noqa: TC003 - used at runtime for asyncio.Event()
import threading


class UseAfterCloseError(Exception):
    """Raised when a closed resource is used."""


class InstrumentedCloseable:
    """Fake resource that tracks construction, open/close state, and use-after-close.

    Useful for detecting stale app.state fallback: if a route handler reads
    a closed generation's client pool from app.state, using it raises
    UseAfterCloseError.

    Attributes:
        construction_count: Class-level counter of instances created.
        close_count: Number of times close() was called on this instance.
    """

    _lock = threading.Lock()
    _construction_count: int = 0

    def __init__(
        self,
        name: str = "closeable",
        *,
        generation_id: int | None = None,
        close_failure: Exception | None = None,
    ) -> None:
        with InstrumentedCloseable._lock:
            InstrumentedCloseable._construction_count += 1
        self._name = name
        self._generation_id = generation_id
        self._close_failure = close_failure
        self._is_closed = False
        self._close_count = 0
        self._close_barrier: asyncio.Event | None = None

    @classmethod
    def reset_construction_count(cls) -> None:
        with cls._lock:
            cls._construction_count = 0

    @classmethod
    def construction_count(cls) -> int:
        with cls._lock:
            return cls._construction_count

    @property
    def is_closed(self) -> bool:
        return self._is_closed

    @property
    def close_count(self) -> int:
        return self._close_count

    @property
    def generation_id(self) -> int | None:
        return self._generation_id

    def use(self) -> None:
        """Use the resource. Raises if closed."""
        if self._is_closed:
            raise UseAfterCloseError(
                f"Use after close on {self._name!r} (generation={self._generation_id})"
            )

    async def close(self) -> None:
        """Close the resource. Tracks close count and optional failure."""
        self._close_count += 1
        if self._close_failure is not None:
            raise self._close_failure
        self._is_closed = True
        if self._close_barrier is not None:
            self._close_barrier.set()

    def set_close_barrier(self, event: asyncio.Event) -> None:
        """Set an event that fires when close() completes."""
        self._close_barrier = event
