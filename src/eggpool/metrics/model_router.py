"""Bounded process-local observability for semantic model routing."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

_MAX_SELECTION_KEYS = 256
_FALLBACK_REASONS = ("timeout", "unavailable", "invalid_output", "repair_failed")


@dataclass(slots=True)
class ModelRouterMetrics:
    """Aggregate semantic-routing counters without request or session data."""

    virtual_requests: int = 0
    selector_decisions: Counter[str] = field(default_factory=Counter[str])
    affinity_hits: int = 0
    affinity_misses: int = 0
    fallback_reasons: Counter[str] = field(default_factory=Counter[str])
    repair_attempts: int = 0
    repair_successes: int = 0
    resolution_latency_count: int = 0
    resolution_latency_total_ms: float = 0.0
    resolution_latency_max_ms: float = 0.0
    selections: Counter[str] = field(default_factory=Counter[str])

    def record_resolution(
        self,
        *,
        virtual_model: str,
        concrete_model: str,
        source: str,
        affinity_hit: bool,
        selector_attempts: int,
        fallback_reason: str | None,
        repair_attempted: bool,
        repair_succeeded: bool,
        latency_ms: float,
    ) -> None:
        """Record one virtual request using only bounded structural labels."""
        self.virtual_requests += 1
        self.selector_decisions[source] += 1
        if affinity_hit:
            self.affinity_hits += 1
        else:
            self.affinity_misses += 1
        if fallback_reason in _FALLBACK_REASONS:
            self.fallback_reasons[fallback_reason] += 1
        if repair_attempted or selector_attempts > 1:
            self.repair_attempts += 1
        if repair_succeeded:
            self.repair_successes += 1

        bounded_latency = max(0.0, latency_ms)
        self.resolution_latency_count += 1
        self.resolution_latency_total_ms += bounded_latency
        self.resolution_latency_max_ms = max(
            self.resolution_latency_max_ms,
            bounded_latency,
        )

        selection_key = f"{virtual_model}|{concrete_model}"
        if (
            selection_key not in self.selections
            and len(self.selections) >= _MAX_SELECTION_KEYS
        ):
            self.selections["__overflow__"] += 1
        else:
            self.selections[selection_key] += 1

    def snapshot(self) -> dict[str, object]:
        """Return a JSON-safe bounded snapshot for runtime diagnostics."""
        return {
            "virtual_requests": self.virtual_requests,
            "selector_decisions": dict(self.selector_decisions),
            "affinity": {
                "hits": self.affinity_hits,
                "misses": self.affinity_misses,
            },
            "fallbacks": {
                reason: self.fallback_reasons.get(reason, 0)
                for reason in _FALLBACK_REASONS
            },
            "repair": {
                "attempts": self.repair_attempts,
                "successes": self.repair_successes,
            },
            "resolution_latency_ms": {
                "count": self.resolution_latency_count,
                "total": self.resolution_latency_total_ms,
                "max": self.resolution_latency_max_ms,
            },
            "selections": dict(self.selections),
        }


__all__ = ["ModelRouterMetrics"]
