"""JSON operation counters for Plan 023 parse/encode instrumentation.

Provides a test-only counting layer around ``eggpool.jsonx`` that tracks
decode/encode operations by direction and lifecycle stage.  Disabled or
near-zero overhead in normal production configuration.

Usage::

    counters = JSONOperationCounters()
    counters.install()  # patches jsonx.loads/dumps_bytes
    # ... run requests ...
    snapshot = counters.snapshot()
    counters.reset()
    counters.uninstall()
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CounterSnapshot:
    """Immutable snapshot of JSON operation counters."""

    request_decode: int = 0
    request_encode: int = 0
    response_decode: int = 0
    response_encode: int = 0
    stream_event_decode: int = 0
    stream_event_encode: int = 0

    @property
    def total_decode(self) -> int:
        return self.request_decode + self.response_decode + self.stream_event_decode

    @property
    def total_encode(self) -> int:
        return self.request_encode + self.response_encode + self.stream_event_encode

    @property
    def total(self) -> int:
        return self.total_decode + self.total_encode

    def to_dict(self) -> dict[str, int]:
        return {
            "request_decode": self.request_decode,
            "request_encode": self.request_encode,
            "response_decode": self.response_decode,
            "response_encode": self.response_encode,
            "stream_event_decode": self.stream_event_decode,
            "stream_event_encode": self.stream_event_encode,
            "total_decode": self.total_decode,
            "total_encode": self.total_encode,
            "total": self.total,
        }


class JSONOperationCounters:
    """Thread-safe JSON operation counters.

    Patches ``eggpool.jsonx.loads`` and ``eggpool.jsonx.dumps_bytes``
    with wrappers that increment category counters.  The wrappers are
    lightweight (one dict lookup + int increment) and the counters are
    only active when ``install()`` has been called.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {
            "request_decode": 0,
            "request_encode": 0,
            "response_decode": 0,
            "response_encode": 0,
            "stream_event_decode": 0,
            "stream_event_encode": 0,
        }
        self._installed = False
        self._original_loads: Any = None
        self._original_dumps_bytes: Any = None
        self._context: str = "request_decode"

    def set_context(self, context: str) -> None:
        """Set the current lifecycle context for subsequent operations.

        Valid contexts: request_decode, request_encode, response_decode,
        response_encode, stream_event_decode, stream_event_encode.
        """
        self._context = context

    def install(self) -> None:
        """Install counter wrappers on eggpool.jsonx."""
        if self._installed:
            return
        import eggpool.jsonx as jsonx

        self._original_loads = jsonx.loads
        self._original_dumps_bytes = jsonx.dumps_bytes

        counters = self._counters
        lock = self._lock

        def _counted_loads(data: Any) -> Any:
            with lock:
                ctx = self._context
                counters[ctx] = counters.get(ctx, 0) + 1
            return self._original_loads(data)

        def _counted_dumps_bytes(obj: Any) -> bytes:
            with lock:
                ctx = self._context.replace("decode", "encode")
                counters[ctx] = counters.get(ctx, 0) + 1
            return self._original_dumps_bytes(obj)

        jsonx.loads = _counted_loads  # type: ignore[assignment]
        jsonx.dumps_bytes = _counted_dumps_bytes  # type: ignore[assignment]
        self._installed = True

    def uninstall(self) -> None:
        """Remove counter wrappers and restore originals."""
        if not self._installed:
            return
        import eggpool.jsonx as jsonx

        jsonx.loads = self._original_loads  # type: ignore[assignment]
        jsonx.dumps_bytes = self._original_dumps_bytes  # type: ignore[assignment]
        self._installed = False

    def snapshot(self) -> CounterSnapshot:
        """Return an immutable snapshot of current counters."""
        with self._lock:
            return CounterSnapshot(**dict(self._counters))

    def reset(self) -> None:
        """Reset all counters to zero."""
        with self._lock:
            for key in self._counters:
                self._counters[key] = 0

    def is_installed(self) -> bool:
        """Return True if counters are currently active."""
        return self._installed


_default_counters = JSONOperationCounters()


def get_json_counters() -> JSONOperationCounters:
    """Return the module-level JSON operation counters."""
    return _default_counters
