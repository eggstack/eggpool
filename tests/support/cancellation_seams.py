"""Deterministic cancellation seam hooks for Plan 023.

Provides eleven named cancellation points along the request lifecycle.
Each seam is a callable that, when set on :class:`CancellationSeamRegistry`,
will trigger ``asyncio.CancelledError`` at exactly the specified point.

Seams are test-only and disabled by default.  Every injected cancellation
test must have a bounded completion wait and a final state audit.

Usage::

    registry = CancellationSeamRegistry()
    registry.activate("after_dispatch_commit")
    # ... run request — CancelledError fires at the named point ...
    assert registry.was_triggered("after_dispatch_commit")
"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import Any


class CancellationPoint(StrEnum):
    """Named cancellation points along the request lifecycle.

    Each value maps to a specific moment where ``CancelledError`` can
    be injected deterministically.
    """

    BEFORE_REQUEST_ROW = "before_request_row"
    AFTER_REQUEST_ROW_BEFORE_SELECTION = "after_request_row_before_selection"
    AFTER_SELECTION_CLAIM = "after_selection_claim"
    AFTER_DISPATCH_COMMIT = "after_dispatch_commit"
    BEFORE_UPSTREAM_SEND = "before_upstream_send"
    AFTER_UPSTREAM_HEADERS = "after_upstream_headers"
    BEFORE_FINALIZATION = "before_finalization"
    DURING_FINALIZATION_TRANSACTION = "during_finalization_transaction"
    AFTER_FINALIZATION_BEFORE_RELEASE = "after_finalization_before_release"
    DURING_RESPONSE_RENDER = "during_response_render"
    MIDSTREAM_AFTER_CHUNK = "midstream_after_chunk"


class CancellationSeamRegistry:
    """Registry of named cancellation seams.

    Seams are activated by name and fire exactly once.  After firing
    the seam becomes inert (no double-fire).  Tests can check which
    seams triggered via :meth:`was_triggered`.
    """

    def __init__(self) -> None:
        self._active: set[str] = set()
        self._triggered: set[str] = set()
        self._call_counts: dict[str, int] = {}

    def activate(self, point: CancellationPoint | str) -> None:
        """Activate a cancellation seam by name.

        The seam fires the next time ``check()`` is called at that point.
        """
        self._active.add(str(point))

    def deactivate(self, point: CancellationPoint | str) -> None:
        """Deactivate a cancellation seam without triggering it."""
        self._active.discard(str(point))

    def deactivate_all(self) -> None:
        """Deactivate all seams."""
        self._active.clear()

    def check(self, point: CancellationPoint | str) -> None:
        """Check if the named seam is active; if so, fire CancelledError.

        Raises ``asyncio.CancelledError`` if the seam is active and has
        not yet fired.  The seam becomes inert after firing.
        """
        key = str(point)
        self._call_counts[key] = self._call_counts.get(key, 0) + 1
        if key in self._active:
            self._active.discard(key)
            self._triggered.add(key)
            raise asyncio.CancelledError(f"Injected cancellation at {key}")

    def was_triggered(self, point: CancellationPoint | str) -> bool:
        """Return True if the named seam has fired."""
        return str(point) in self._triggered

    def any_triggered(self) -> bool:
        """Return True if any seam has fired."""
        return bool(self._triggered)

    def call_count(self, point: CancellationPoint | str) -> int:
        """Return the number of times ``check()`` was called for a point."""
        return self._call_counts.get(str(point), 0)

    def reset(self) -> None:
        """Reset all state."""
        self._active.clear()
        self._triggered.clear()
        self._call_counts.clear()

    def summary(self) -> dict[str, Any]:
        """Return a summary dict for diagnostics."""
        return {
            "active": sorted(self._active),
            "triggered": sorted(self._triggered),
            "call_counts": dict(self._call_counts),
        }


# Module-level singleton for convenience in tests
_default_registry = CancellationSeamRegistry()


def get_cancellation_registry() -> CancellationSeamRegistry:
    """Return the module-level cancellation seam registry."""
    return _default_registry


def inject_cancellation(
    point: CancellationPoint | str,
    registry: CancellationSeamRegistry | None = None,
) -> None:
    """Activate a cancellation seam on the default or given registry."""
    reg = registry or _default_registry
    reg.activate(point)


def check_cancellation(
    point: CancellationPoint | str,
    registry: CancellationSeamRegistry | None = None,
) -> None:
    """Check (and fire if active) a cancellation seam."""
    reg = registry or _default_registry
    reg.check(point)
