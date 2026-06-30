"""Artificial Analysis model-info source adapter.

Fetches structured model/benchmark records from the Artificial Analysis
API and emits ``SourceModelRecord`` observations.  The adapter is optional
and requires an API key.  Failures are recorded as source-health errors
and do not break startup, catalog refresh, or routing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import httpx

from eggpool.errors import ModelInfoSourceFetchError
from eggpool.model_info.types import BenchmarkObservation, SourceModelRecord

if TYPE_CHECKING:
    from eggpool.model_info.sources.openrouter import ModelInfoHttpClient
    from eggpool.models.config import ModelInfoSourceConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TTL cache for the raw Artificial Analysis catalog
# ---------------------------------------------------------------------------


class _AATTLCache:
    """TTL cache indexed by source model ID."""

    def __init__(self, ttl_seconds: int, max_entries: int = 4096) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._data: OrderedDict[str, dict[str, object]] = OrderedDict()
        self._fetched_at: float = 0.0
        self._lock: asyncio.Lock = asyncio.Lock()

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    @property
    def is_fresh(self) -> bool:
        if self._fetched_at == 0.0:
            return False
        return (time.monotonic() - self._fetched_at) < self._ttl

    def store(self, entries: dict[str, dict[str, object]]) -> None:
        self._data = OrderedDict(entries)
        self._fetched_at = time.monotonic()
        self._evict_to_capacity()

    def _evict_to_capacity(self) -> None:
        while len(self._data) > self._max_entries:
            self._data.popitem(last=False)

    def get(self, key: str) -> dict[str, object] | None:
        return self._data.get(key)

    def snapshot(self) -> dict[str, dict[str, object]]:
        return dict(self._data)


# ---------------------------------------------------------------------------
# Artificial Analysis model-info source
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtificialAnalysisEntry:
    """Parsed Artificial Analysis model entry."""

    source_model_id: str
    display_name: str | None = None
    benchmarks: tuple[BenchmarkObservation, ...] = ()
    raw: dict[str, object] = field(default_factory=dict[str, object])


class ArtificialAnalysisSource:
    """Artificial Analysis API as a model-info observation source."""

    name = "artificial_analysis"

    def __init__(
        self,
        *,
        config: ModelInfoSourceConfig,
        client: ModelInfoHttpClient,
        cache: _AATTLCache | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._cache = cache or _AATTLCache(
            ttl_seconds=config.ttl_seconds, max_entries=config.max_entries
        )

    @property
    def priority(self) -> int:
        return self._config.priority

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"User-Agent": "eggpool/1.0"}
        api_key = self._config.resolved_api_key
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _url(self) -> str:
        base = self._config.base_url or "https://api.artificialanalysis.ai"
        path = self._config.options.get("models_path", "/v1/models")
        return f"{base.rstrip('/')}{path}"

    def _benchmarks_url(self) -> str:
        base = self._config.base_url or "https://api.artificialanalysis.ai"
        path = self._config.options.get("benchmarks_path", "/v1/benchmarks")
        return f"{base.rstrip('/')}{path}"

    async def fetch_all(self) -> list[SourceModelRecord]:
        """Fetch the full AA catalog and return ``SourceModelRecord``s."""
        indexed = await self._fetch_indexed()
        now = datetime.now(UTC)
        records: list[SourceModelRecord] = []
        for source_model_id, raw in indexed.items():
            record = _parse_entry_to_record(source_model_id, raw, now)
            records.append(record)
        return records

    async def fetch_one(
        self, model_id: str, *, provider_id: str | None = None
    ) -> SourceModelRecord | None:
        """Fetch a single model by source model ID."""
        indexed = await self._fetch_indexed()
        raw = indexed.get(model_id)
        if raw is None:
            return None
        now = datetime.now(UTC)
        return _parse_entry_to_record(model_id, raw, now)

    async def _fetch_indexed(self) -> dict[str, dict[str, object]]:
        """Return the catalog indexed by source model ID, using cache when fresh."""
        if self._cache.is_fresh:
            return self._cache.snapshot()
        async with self._cache.lock:
            if self._cache.is_fresh:
                return self._cache.snapshot()
            try:
                response = await self._client.get(self._url(), headers=self._headers())
                response.raise_for_status()
                payload_obj: object = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise ModelInfoSourceFetchError(
                    f"Artificial Analysis model-info fetch failed: {exc}"
                ) from exc
            entries = _parse_catalog_payload(payload_obj)
            self._cache.store(entries)
            return entries


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_catalog_payload(payload: object) -> dict[str, dict[str, object]]:
    """Parse the AA /models response into a dict keyed by model ID."""
    entries: dict[str, dict[str, object]] = {}
    if not isinstance(payload, dict):
        return entries
    data_dict: dict[str, Any] = cast("dict[str, Any]", payload)
    data_obj: object = data_dict.get("data", data_dict.get("models", []))
    if not isinstance(data_obj, list):
        # Some AA responses may be a flat dict of model entries
        if isinstance(data_dict.get("slug"), str):
            slug = data_dict["slug"]
            entries[slug] = data_dict
        return entries
    for raw_obj in cast("list[object]", data_obj):
        if not isinstance(raw_obj, dict):
            continue
        raw_dict: dict[str, Any] = cast("dict[str, Any]", raw_obj)
        model_id_obj: object = raw_dict.get("id") or raw_dict.get("slug")
        if not isinstance(model_id_obj, str) or not model_id_obj:
            continue
        entries[model_id_obj] = raw_dict
    return entries


def _parse_entry_to_record(
    source_model_id: str,
    raw: dict[str, object],
    now: datetime,
) -> SourceModelRecord:
    """Convert a raw AA model dict into a ``SourceModelRecord``."""
    raw_hash = hashlib.sha256(
        json.dumps(raw, sort_keys=True, default=str).encode()
    ).hexdigest()

    display_name = (
        _opt_str(raw, "name") or _opt_str(raw, "display_name") or source_model_id
    )

    # Parse benchmarks from the entry
    benchmarks = _parse_benchmarks(raw, source_model_id)

    normalized: dict[str, object] = {
        "source_model_id": source_model_id,
        "display_name": display_name,
        "benchmarks": [
            {
                "name": b.benchmark_name,
                "score": b.score,
                "rank": b.rank,
                "percentile": b.percentile,
                "source": b.source,
                "notes": b.notes,
            }
            for b in benchmarks
        ],
    }

    return SourceModelRecord(
        source="artificial_analysis",
        source_model_id=source_model_id,
        observed_at=now,
        raw_hash=raw_hash,
        raw_payload=raw,
        normalized=normalized,
        display_name=display_name,
        benchmarks=benchmarks,
        confidence=0.7,
        sparse=not bool(display_name and display_name != source_model_id),
        notes=("Artificial Analysis intelligence index",),
    )


def _parse_benchmarks(
    raw: dict[str, object], source_model_id: str
) -> tuple[BenchmarkObservation, ...]:
    """Extract benchmark observations from an AA entry."""
    benchmarks: list[BenchmarkObservation] = []

    # Intelligence index (AA's primary composite score)
    ii = raw.get("intelligence_index") or raw.get("score")
    if isinstance(ii, (int, float)):
        benchmarks.append(
            BenchmarkObservation(
                benchmark_name="Artificial Analysis Intelligence Index",
                score=float(ii),
                source="artificial_analysis",
                notes="Composite intelligence index",
            )
        )

    # Speed index
    si = raw.get("speed_index")
    if isinstance(si, (int, float)):
        benchmarks.append(
            BenchmarkObservation(
                benchmark_name="Artificial Analysis Speed Index",
                score=float(si),
                source="artificial_analysis",
                notes="Composite speed index",
            )
        )

    # Quality index
    qi = raw.get("quality_index")
    if isinstance(qi, (int, float)):
        benchmarks.append(
            BenchmarkObservation(
                benchmark_name="Artificial Analysis Quality Index",
                score=float(qi),
                source="artificial_analysis",
                notes="Composite quality index",
            )
        )

    # Generic benchmarks array if present
    bench_arr = raw.get("benchmarks")
    if isinstance(bench_arr, list):
        for item in cast("list[object]", bench_arr):
            if not isinstance(item, dict):
                continue
            item_dict: dict[str, Any] = cast("dict[str, Any]", item)
            name = item_dict.get("name") or item_dict.get("benchmark")
            if not isinstance(name, str) or not name:
                continue
            score = item_dict.get("score")
            rank = item_dict.get("rank")
            percentile = item_dict.get("percentile")
            benchmarks.append(
                BenchmarkObservation(
                    benchmark_name=name,
                    score=float(score) if isinstance(score, (int, float)) else None,
                    rank=int(rank) if isinstance(rank, (int, float)) else None,
                    percentile=(
                        float(percentile)
                        if isinstance(percentile, (int, float))
                        else None
                    ),
                    source="artificial_analysis",
                )
            )

    return tuple(benchmarks)


def _opt_str(raw: dict[str, object], key: str) -> str | None:
    val = raw.get(key)
    return val if isinstance(val, str) and val else None
