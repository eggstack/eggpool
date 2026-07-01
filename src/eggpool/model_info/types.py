"""Typed records for the model-info subsystem."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from datetime import date, datetime

ModelInfoStatus = Literal[
    "fresh",
    "partial",
    "sparse_new",
    "stale",
    "conflicting",
    "unmatched",
    "source_unavailable",
    "manual_override",
    "withdrawn",
]

# Status priority for ordering (lower = higher priority for refresh).
STATUS_PRIORITY: dict[str, int] = {
    "conflicting": 0,
    "sparse_new": 1,
    "partial": 2,
    "stale": 3,
    "fresh": 4,
    "unmatched": 5,
    "source_unavailable": 6,
    "manual_override": 7,
    "withdrawn": 8,
}


@dataclass(frozen=True)
class BenchmarkObservation:
    """A single benchmark score observation for a model."""

    benchmark_name: str
    score: float | None = None
    rank: int | None = None
    percentile: float | None = None
    version: str | None = None
    source: str = "unknown"
    observed_at: datetime | None = None
    notes: str | None = None


@dataclass(frozen=True)
class SourceModelRecord:
    """A normalized observation from a single source about a model."""

    source: str
    source_model_id: str
    observed_at: datetime
    raw_hash: str
    raw_payload: dict[str, object]
    normalized: dict[str, object]
    aliases: tuple[str, ...] = ()
    provider_id: str | None = None
    model_id: str | None = None
    display_name: str | None = None
    family: str | None = None
    context_window: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    modalities: frozenset[str] = frozenset()
    supports_tools: bool | None = None
    supports_reasoning: bool | None = None
    thinking_capability: dict[str, object] | None = None
    input_price_per_1k: float | None = None
    output_price_per_1k: float | None = None
    benchmarks: tuple[BenchmarkObservation, ...] = ()
    release_date: date | None = None
    license: str | None = None
    confidence: float = 0.5
    sparse: bool = False
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CanonicalModelInfo:
    """The canonical summary record for a model."""

    model_id: str
    status: ModelInfoStatus
    summary: str | None
    sparse: bool
    detail: dict[str, object]
    provenance: dict[str, object]
    conflicts: dict[str, object]
    first_seen_at: datetime
    last_seen_at: datetime
    last_refreshed_at: datetime | None
    next_refresh_at: datetime | None
