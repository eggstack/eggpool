"""Artificial Analysis model-info source adapter.

Fetches structured model/benchmark records from the Artificial Analysis
API and emits ``SourceModelRecord`` observations.  The adapter is optional
and requires an API key.  Failures are recorded as source-health errors
and do not break startup, catalog refresh, or routing.
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import httpx

from eggpool.errors import ModelInfoSourceFetchError
from eggpool.jsonx import dumps_str
from eggpool.model_info.sources.base import SourceTTLCache
from eggpool.model_info.types import BenchmarkObservation, SourceModelRecord

if TYPE_CHECKING:
    from eggpool.model_info.sources.openrouter import ModelInfoHttpClient
    from eggpool.models.config import ModelInfoSourceConfig

logger = logging.getLogger(__name__)


_AATTLCache = SourceTTLCache


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
            # The current AA Data API documents x-api-key.  Keep the
            # Authorization form too for older compatible deployments and
            # test doubles; servers ignore the unknown alternate scheme.
            headers["x-api-key"] = api_key
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _url(self) -> str:
        base = self._config.base_url or "https://artificialanalysis.ai/api/v2"
        path = self._config.options.get("models_path", "/language/models")
        if not isinstance(path, str) or not path:
            path = "/language/models"
        return f"{base.rstrip('/')}{path}"

    def _free_url(self) -> str:
        """Return the Free-tier-compatible language model endpoint."""
        base = self._config.base_url or "https://artificialanalysis.ai/api/v2"
        path = self._config.options.get("free_models_path", "/language/models/free")
        if not isinstance(path, str) or not path:
            path = "/language/models/free"
        return f"{base.rstrip('/')}{path}"

    def _benchmarks_url(self) -> str:
        base = self._config.base_url or "https://artificialanalysis.ai/api/v2"
        path = self._config.options.get("benchmarks_path", "/language/models")
        if not isinstance(path, str) or not path:
            path = "/language/models"
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
                # The full endpoint is Pro+ according to the current AA API
                # contract.  A valid Free-tier key can still retrieve the
                # public headline indices from the /free sibling.
                if (
                    response.status_code in {403, 404}
                    and self._free_url() != self._url()
                ):
                    response = await self._client.get(
                        self._free_url(), headers=self._headers()
                    )
                response.raise_for_status()
                payload_obj: object = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise ModelInfoSourceFetchError(
                    f"Artificial Analysis model-info fetch failed: {exc}"
                ) from exc
            entries = _parse_catalog_payload(payload_obj)
            if not entries:
                self._cache.invalidate()
                logger.warning(
                    "Artificial Analysis model-info fetch returned no catalog "
                    "entries; not caching the empty response"
                )
                return entries
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
        slug_obj = (
            data_dict.get("slug")
            or data_dict.get("model_slug")
            or data_dict.get("model_permaslug")
            or data_dict.get("id")
        )
        if isinstance(slug_obj, str):
            slug = slug_obj
            entries[slug] = data_dict
        return entries
    for raw_obj in cast("list[object]", data_obj):
        if not isinstance(raw_obj, dict):
            continue
        raw_dict: dict[str, Any] = cast("dict[str, Any]", raw_obj)
        # Prefer AA's human-readable slug.  Some versions also expose an
        # opaque UUID as ``id``; using that as the primary identity makes
        # otherwise-correct local model names impossible to match.
        model_id_obj: object = (
            raw_dict.get("slug")
            or raw_dict.get("model_slug")
            or raw_dict.get("model_permaslug")
            or raw_dict.get("id")
        )
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
        dumps_str(raw, sort_keys=True, default=str).encode()
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

    aliases: list[str] = []
    for key in ("id", "slug", "model_slug", "model_permaslug"):
        value = raw.get(key)
        if isinstance(value, str) and value and value != source_model_id:
            aliases.append(value)

    return SourceModelRecord(
        source="artificial_analysis",
        source_model_id=source_model_id,
        observed_at=now,
        raw_hash=raw_hash,
        raw_payload=raw,
        normalized=normalized,
        display_name=display_name,
        aliases=tuple(sorted(set(aliases))),
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
    evaluations = raw.get("evaluations")
    evaluation_map = (
        cast("dict[str, object]", evaluations) if isinstance(evaluations, dict) else {}
    )

    ii = _first_numeric(
        raw.get("intelligence_index"),
        raw.get("score"),
        evaluation_map.get("artificial_analysis_intelligence_index"),
        evaluation_map.get("intelligence_index"),
    )
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
    si = _first_numeric(
        raw.get("speed_index"),
        evaluation_map.get("speed_index"),
    )
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
    qi = _first_numeric(
        raw.get("quality_index"),
        evaluation_map.get("quality_index"),
    )
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

    # The current AA API puts the composite and individual scores under
    # ``evaluations``.  Preserve every finite numeric evaluation so new
    # benchmark names do not require a code release.
    emitted_names = {b.benchmark_name.casefold() for b in benchmarks}
    for key, value in evaluation_map.items():
        score = _first_numeric(value)
        if score is None:
            continue
        label = str(key).removeprefix("artificial_analysis_").replace("_", " ")
        label = " ".join(part.capitalize() for part in label.split())
        if not label:
            continue
        name = f"Artificial Analysis {label}"
        if name.casefold() in emitted_names:
            continue
        benchmarks.append(
            BenchmarkObservation(
                benchmark_name=name,
                score=score,
                source="artificial_analysis",
                notes="Published by Artificial Analysis",
            )
        )
        emitted_names.add(name.casefold())

    return tuple(benchmarks)


def _first_numeric(*values: object) -> float | None:
    """Return the first finite numeric value from a list of candidates."""
    for value in values:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric = float(value)
            if math.isfinite(numeric):
                return numeric
        if isinstance(value, str):
            try:
                numeric = float(value.strip())
            except ValueError:
                continue
            if math.isfinite(numeric):
                return numeric
    return None


def _opt_str(raw: dict[str, object], key: str) -> str | None:
    val = raw.get(key)
    return val if isinstance(val, str) and val else None
