"""Plan 025 — typed failure-effects and bounded model quarantine counters.

Tracks per-observation failure-effects decisions: request-local validation,
bounded quarantine events, and terminal withdrawals.  The counters are
the only way the dashboard distinguishes the three categories without
re-scoring effects from raw status codes.

Usage::

    from eggpool.metrics.failure_effects import (
        FailureEffectsCounter,
        get_counter,
        record_failure_effects,
    )

    # Direct increment
    counter = get_counter()
    await counter.increment_quarantine(reason="model_404", provenance="runtime_http")

    # Convenience wrapper inspects an event
    await record_failure_effects(event)
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)
_MAX_COUNTER_KEYS = 1024


_VALID_CATEGORIES: frozenset[str] = frozenset(
    {
        "request_local",
        "quarantine_suspected",
        "quarantine_promoted",
        "quarantine_cleared",
        "terminal_withdrawal",
        "circuit_penalty",
        "backoff_persisted",
        "probe_released",
    }
)


@dataclass(frozen=True, slots=True)
class FailureEffectsEvent:
    """Immutable per-observation failure-effects metadata."""

    category: str
    reason: str
    evidence_class: str
    source: str
    provider_id: str | None


class FailureEffectsCounter:
    """Asyncio-safe in-memory counter for failure-effects decisions.

    Counters are keyed by a pipe-delimited label-tuple string.  An
    :class:`asyncio.Lock` serialises mutations so concurrent tasks never
    corrupt the counter dict.  Use :meth:`snapshot` to obtain a
    point-in-time view suitable for ``/metrics`` / ``/runtime``.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._counters: dict[str, int] = {}

    def _increment(self, key: str) -> None:
        """Increment a key while bounding label cardinality."""
        if key not in self._counters and len(self._counters) >= _MAX_COUNTER_KEYS:
            self._counters.pop(next(iter(self._counters)))
        self._counters[key] = self._counters.get(key, 0) + 1

    async def increment_request_local(
        self,
        *,
        reason: str,
        evidence_class: str,
        source: str,
    ) -> None:
        """Increment the request-local counter."""
        await self._bump(
            category="request_local",
            reason=reason,
            evidence_class=evidence_class,
            source=source,
            provider_id="unknown",
        )

    async def increment_quarantine_suspected(
        self,
        *,
        reason: str,
        evidence_class: str,
        source: str,
        provider_id: str,
    ) -> None:
        """Increment the suspected-quarantine counter."""
        await self._bump(
            category="quarantine_suspected",
            reason=reason,
            evidence_class=evidence_class,
            source=source,
            provider_id=provider_id,
        )

    async def increment_quarantine_promoted(
        self,
        *,
        reason: str,
        evidence_class: str,
        source: str,
        provider_id: str,
    ) -> None:
        """Increment the promoted-quarantine counter."""
        await self._bump(
            category="quarantine_promoted",
            reason=reason,
            evidence_class=evidence_class,
            source=source,
            provider_id=provider_id,
        )

    async def increment_quarantine_cleared(
        self,
        *,
        reason: str,
        evidence_class: str,
        source: str,
        provider_id: str,
    ) -> None:
        """Increment the quarantine-cleared counter."""
        await self._bump(
            category="quarantine_cleared",
            reason=reason,
            evidence_class=evidence_class,
            source=source,
            provider_id=provider_id,
        )

    async def increment_terminal_withdrawal(
        self,
        *,
        reason: str,
        evidence_class: str,
        source: str,
        provider_id: str,
    ) -> None:
        """Increment the terminal-withdrawal counter."""
        await self._bump(
            category="terminal_withdrawal",
            reason=reason,
            evidence_class=evidence_class,
            source=source,
            provider_id=provider_id,
        )

    async def increment_circuit_penalty(
        self,
        *,
        reason: str,
        evidence_class: str,
        source: str,
        provider_id: str,
    ) -> None:
        """Increment the circuit-penalty counter."""
        await self._bump(
            category="circuit_penalty",
            reason=reason,
            evidence_class=evidence_class,
            source=source,
            provider_id=provider_id,
        )

    async def increment_backoff_persisted(
        self,
        *,
        reason: str,
        evidence_class: str,
        source: str,
        provider_id: str,
    ) -> None:
        """Increment the backoff-persisted counter."""
        await self._bump(
            category="backoff_persisted",
            reason=reason,
            evidence_class=evidence_class,
            source=source,
            provider_id=provider_id,
        )

    async def increment_probe_released(
        self,
        *,
        reason: str,
        evidence_class: str,
        source: str,
    ) -> None:
        """Increment the probe-released counter."""
        await self._bump(
            category="probe_released",
            reason=reason,
            evidence_class=evidence_class,
            source=source,
            provider_id="unknown",
        )

    async def _bump(
        self,
        *,
        category: str,
        reason: str,
        evidence_class: str,
        source: str,
        provider_id: str,
    ) -> None:
        key = f"{category}|{reason}|{evidence_class}|{source}|{provider_id}"
        async with self._lock:
            self._increment(key)

    async def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot.

        Keys:
            ``total``
                Sum of all counter values.
            ``counters``
                Dict mapping each label-tuple key to its integer value.
            ``label_breakdown``
                Per-category breakdown keyed by the first label segment.
            ``categories``
                Dict of category totals for dashboard display.
        """
        async with self._lock:
            counters_copy = dict(self._counters)

        total = sum(counters_copy.values())
        label_breakdown: dict[str, dict[str, int]] = {}
        category_totals: dict[str, int] = {}
        for key, value in counters_copy.items():
            category = key.split("|", 1)[0]
            label_breakdown.setdefault(category, {})[key] = value
            category_totals[category] = category_totals.get(category, 0) + value

        return {
            "total": total,
            "counters": counters_copy,
            "label_breakdown": label_breakdown,
            "categories": category_totals,
        }

    async def reset(self) -> None:
        """Clear all counters.  Intended for testing only."""
        async with self._lock:
            self._counters.clear()


_counter: FailureEffectsCounter | None = None
_counter_lock = threading.Lock()


def get_counter() -> FailureEffectsCounter:
    """Return the module-level :class:`FailureEffectsCounter` singleton.

    The instance is created lazily on first call.
    """
    global _counter  # noqa: PLW0603
    if _counter is None:
        with _counter_lock:
            if _counter is None:
                _counter = FailureEffectsCounter()
    return _counter


async def record_failure_effects(event: FailureEffectsEvent) -> None:
    """Inspect *event* and dispatch to the appropriate counter method.

    Convenience wrapper for callers that have already assembled a
    :class:`FailureEffectsEvent`.  Categories outside the known set are
    silently dropped to keep the counter low-cardinality.
    """
    if event.category not in _VALID_CATEGORIES:
        return
    counter = get_counter()
    provider_id = event.provider_id or "unknown"
    if event.category == "request_local":
        await counter.increment_request_local(
            reason=event.reason,
            evidence_class=event.evidence_class,
            source=event.source,
        )
    elif event.category == "quarantine_suspected":
        await counter.increment_quarantine_suspected(
            reason=event.reason,
            evidence_class=event.evidence_class,
            source=event.source,
            provider_id=provider_id,
        )
    elif event.category == "quarantine_promoted":
        await counter.increment_quarantine_promoted(
            reason=event.reason,
            evidence_class=event.evidence_class,
            source=event.source,
            provider_id=provider_id,
        )
    elif event.category == "quarantine_cleared":
        await counter.increment_quarantine_cleared(
            reason=event.reason,
            evidence_class=event.evidence_class,
            source=event.source,
            provider_id=provider_id,
        )
    elif event.category == "terminal_withdrawal":
        await counter.increment_terminal_withdrawal(
            reason=event.reason,
            evidence_class=event.evidence_class,
            source=event.source,
            provider_id=provider_id,
        )
    elif event.category == "circuit_penalty":
        await counter.increment_circuit_penalty(
            reason=event.reason,
            evidence_class=event.evidence_class,
            source=event.source,
            provider_id=provider_id,
        )
    elif event.category == "backoff_persisted":
        await counter.increment_backoff_persisted(
            reason=event.reason,
            evidence_class=event.evidence_class,
            source=event.source,
            provider_id=provider_id,
        )
    elif event.category == "probe_released":
        await counter.increment_probe_released(
            reason=event.reason,
            evidence_class=event.evidence_class,
            source=event.source,
        )
