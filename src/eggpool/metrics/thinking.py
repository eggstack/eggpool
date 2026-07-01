"""Thinking/reasoning observability counters for request processing.

Tracks per-request thinking-related decisions made by the transcoder and
routing layers.  Counters are low-cardinality (protocol, decision,
capability_status, provider_id) and stored as an in-memory dict keyed by
pipe-delimited label tuples.

Usage::

    from eggpool.metrics.thinking import get_counter, record_thinking_event

    # Direct increment
    counter = get_counter()
    await counter.increment_transcoded(
        client_protocol="openai",
        upstream_protocol="anthropic",
        provider_id="anthropic-prod",
    )

    # Convenience wrapper inspects the event and dispatches
    await record_thinking_event(event)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event dataclass
# ---------------------------------------------------------------------------

_VALID_DECISIONS: frozenset[str] = frozenset(
    {
        "transcoded",
        "dropped",
        "rejected",
        "clamped",
        "unknown_capability",
        "unsupported_capability",
        "passthrough",
        "none",
    }
)


@dataclass(frozen=True, slots=True)
class ThinkingMetricEvent:
    """Immutable per-request thinking trace metadata."""

    requested: bool
    client_protocol: str
    request_fields: list[str]
    requested_effort: str | None
    resolved_budget_tokens: int | None
    budget_clamped: bool
    capability_status: str | None
    capability_source: str | None
    upstream_protocol: str | None
    upstream_fields: list[str]
    decision: str  # one of _VALID_DECISIONS


# ---------------------------------------------------------------------------
# Counter class
# ---------------------------------------------------------------------------


class ThinkingMetricsCounter:
    """Thread-safe (asyncio-safe) in-memory counter for thinking decisions.

    Counters are keyed by a pipe-delimited label tuple string
    (``"label1|label2|..."``).  An :class:`asyncio.Lock` serialises
    mutations so concurrent tasks never corrupt the counter dict.

    Use :meth:`snapshot` to obtain a point-in-time view suitable for
    a ``/metrics`` or ``/runtime`` endpoint.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._counters: dict[str, int] = {}

    # -- Increment helpers ---------------------------------------------------

    async def increment_requested(self, *, client_protocol: str) -> None:
        key = f"requested|{client_protocol}"
        async with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1

    async def increment_transcoded(
        self,
        *,
        client_protocol: str,
        upstream_protocol: str,
        provider_id: str,
    ) -> None:
        key = f"transcoded|{client_protocol}|{upstream_protocol}|{provider_id}"
        async with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1

    async def increment_dropped(
        self,
        *,
        client_protocol: str,
        upstream_protocol: str,
        reason: str,
    ) -> None:
        key = f"dropped|{client_protocol}|{upstream_protocol}|{reason}"
        async with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1

    async def increment_rejected(
        self,
        *,
        client_protocol: str,
        capability_status: str,
    ) -> None:
        key = f"rejected|{client_protocol}|{capability_status}"
        async with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1

    async def increment_unknown_capability(self, *, client_protocol: str) -> None:
        key = f"unknown_capability|{client_protocol}"
        async with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1

    async def increment_unsupported_capability(self, *, client_protocol: str) -> None:
        key = f"unsupported_capability|{client_protocol}"
        async with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1

    async def increment_budget_clamped(
        self, *, client_protocol: str, provider_id: str
    ) -> None:
        key = f"budget_clamped|{client_protocol}|{provider_id}"
        async with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1

    async def increment_stream_delta(
        self, *, client_protocol: str, upstream_protocol: str
    ) -> None:
        key = f"stream_delta|{client_protocol}|{upstream_protocol}"
        async with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1

    async def increment_response_block(
        self, *, client_protocol: str, upstream_protocol: str
    ) -> None:
        key = f"response_block|{client_protocol}|{upstream_protocol}"
        async with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1

    # -- Query / lifecycle ---------------------------------------------------

    async def snapshot(self) -> dict[str, Any]:
        """Return a structured snapshot of all counters.

        The returned dict contains:

        ``total``
            Sum of all counter values.
        ``counters``
            Dict mapping each label-tuple key to its integer value.
        ``label_breakdown``
            Per-category breakdown keyed by the first label segment
            (e.g. ``"requested"``, ``"transcoded"``) with the
            per-key values nested underneath.
        """
        async with self._lock:
            counters_copy = dict(self._counters)

        total = sum(counters_copy.values())

        # Build per-category breakdowns
        label_breakdown: dict[str, dict[str, int]] = {}
        for key, value in counters_copy.items():
            category = key.split("|", 1)[0]
            if category not in label_breakdown:
                label_breakdown[category] = {}
            label_breakdown[category][key] = value

        return {
            "total": total,
            "counters": counters_copy,
            "label_breakdown": label_breakdown,
        }

    async def reset(self) -> None:
        """Clear all counters.  Intended for testing only."""
        async with self._lock:
            self._counters.clear()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_counter: ThinkingMetricsCounter | None = None


def get_counter() -> ThinkingMetricsCounter:
    """Return the module-level :class:`ThinkingMetricsCounter` singleton.

    The instance is created lazily on first call.
    """
    global _counter  # noqa: PLW0603
    if _counter is None:
        _counter = ThinkingMetricsCounter()
    return _counter


# ---------------------------------------------------------------------------
# Convenience recorder
# ---------------------------------------------------------------------------


async def record_thinking_event(event: ThinkingMetricEvent) -> None:
    """Inspect *event* and dispatch to the appropriate counter method.

    This is the primary entry-point for request processing code that
    has already assembled a :class:`ThinkingMetricEvent`.
    """
    counter = get_counter()

    if event.requested:
        await counter.increment_requested(
            client_protocol=event.client_protocol,
        )

    decision = event.decision
    if decision == "transcoded":
        await counter.increment_transcoded(
            client_protocol=event.client_protocol,
            upstream_protocol=event.upstream_protocol or "unknown",
            provider_id="unknown",
        )
    elif decision == "dropped":
        await counter.increment_dropped(
            client_protocol=event.client_protocol,
            upstream_protocol=event.upstream_protocol or "unknown",
            reason="capability_mismatch",
        )
    elif decision == "rejected":
        await counter.increment_rejected(
            client_protocol=event.client_protocol,
            capability_status=event.capability_status or "unknown",
        )
    elif decision == "unknown_capability":
        await counter.increment_unknown_capability(
            client_protocol=event.client_protocol,
        )
    elif decision == "unsupported_capability":
        await counter.increment_unsupported_capability(
            client_protocol=event.client_protocol,
        )
    elif decision in ("passthrough", "none", "clamped"):
        # clamped is tracked separately when budget_clamped is True
        pass

    if event.budget_clamped:
        await counter.increment_budget_clamped(
            client_protocol=event.client_protocol,
            provider_id="unknown",
        )
