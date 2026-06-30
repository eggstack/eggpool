"""Hugging Face model-info source adapter.

Fetches model metadata through the Hugging Face Hub API for exact
source_model_id matches.  Only attempts enrichment for models that
appear likely open-weight/open-source by exact alias or configured
mapping.  Does not search the Hub by arbitrary model name.

Design constraints (from the phase-5 plan):

- Only exact source_model_id matching or curated aliases.
- Does not search the Hub by arbitrary model name.
- Model card text is summarized, not stored verbatim.
- Failures are recorded as source-health errors and do not break
  startup, catalog refresh, or routing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import httpx

from eggpool.errors import ModelInfoSourceFetchError
from eggpool.model_info.types import SourceModelRecord

if TYPE_CHECKING:
    from eggpool.model_info.sources.openrouter import ModelInfoHttpClient
    from eggpool.models.config import ModelInfoSourceConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TTL cache for the raw Hugging Face catalog
# ---------------------------------------------------------------------------


class _HFTTLCache:
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
# Hugging Face model-info source
# ---------------------------------------------------------------------------


class HuggingFaceSource:
    """Hugging Face Hub API as a model-info observation source."""

    name = "huggingface"

    def __init__(
        self,
        *,
        config: ModelInfoSourceConfig,
        client: ModelInfoHttpClient,
        cache: _HFTTLCache | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._cache = cache or _HFTTLCache(
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

    def _model_url(self, model_id: str) -> str:
        base = self._config.base_url or "https://huggingface.co"
        return f"{base.rstrip('/')}/api/models/{model_id}"

    async def fetch_all(self) -> list[SourceModelRecord]:
        """Hugging Face is per-model; fetch_all returns cached entries."""
        indexed = await self._fetch_all_cached()
        now = datetime.now(UTC)
        records: list[SourceModelRecord] = []
        for source_model_id, raw in indexed.items():
            record = _parse_hf_entry(source_model_id, raw, now)
            records.append(record)
        return records

    async def fetch_one(
        self, model_id: str, *, provider_id: str | None = None
    ) -> SourceModelRecord | None:
        """Fetch a single model by exact Hugging Face model ID."""
        # Check cache first
        cached = self._cache.get(model_id)
        if cached is not None:
            now = datetime.now(UTC)
            return _parse_hf_entry(model_id, cached, now)

        # Fetch from API
        try:
            url = self._model_url(model_id)
            response = await self._client.get(url, headers=self._headers())
            if response.status_code == 404:
                return None
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelInfoSourceFetchError(
                f"Hugging Face model-info fetch failed for {model_id}: {exc}"
            ) from exc

        # Store in cache
        async with self._cache.lock:
            self._cache.store({model_id: payload})

        now = datetime.now(UTC)
        return _parse_hf_entry(model_id, payload, now)

    async def _fetch_all_cached(self) -> dict[str, dict[str, object]]:
        """Return cached entries. HuggingFace is per-model, not bulk."""
        return self._cache.snapshot()


def _parse_hf_entry(
    source_model_id: str,
    raw: dict[str, object],
    now: datetime,
) -> SourceModelRecord:
    """Convert a raw Hugging Face model dict into a ``SourceModelRecord``."""
    raw_hash = hashlib.sha256(
        json.dumps(raw, sort_keys=True, default=str).encode()
    ).hexdigest()

    # Extract structured fields
    tags = _opt_list(raw, "tags")
    license_str = _opt_str(raw, "license") or _opt_str(raw, "license_name")
    pipeline_tag = _opt_str(raw, "pipeline_tag")
    library_name = _opt_str(raw, "library_name")
    model_type = _opt_str(raw, "model_type")
    downloads = _opt_int(raw, "downloads")
    likes = _opt_int(raw, "likes")

    # Card metadata (compact, not full text)
    card_data = raw.get("card_data")
    card_metadata: dict[str, object] = {}
    if isinstance(card_data, dict):
        card_dict: dict[str, object] = cast("dict[str, object]", card_data)
        for key in ("license", "language", "tags", "datasets", "metrics"):
            val = card_dict.get(key)
            if val is not None:
                card_metadata[key] = val

    # Determine display name
    display_name = _opt_str(raw, "name") or _opt_str(raw, "modelId") or source_model_id

    # Determine modalities from tags/pipeline
    modalities: frozenset[str] = frozenset()
    if pipeline_tag:
        if "text-generation" in pipeline_tag or "text2text-generation" in pipeline_tag:
            modalities = frozenset({"text"})
        elif "image" in pipeline_tag or "vision" in pipeline_tag:
            modalities = frozenset({"text", "image"})

    # Build license note
    license_notes: list[str] = []
    if license_str:
        license_notes.append(f"License: {license_str}")
    if downloads is not None:
        license_notes.append(f"{downloads:,} downloads")
    if likes is not None:
        license_notes.append(f"{likes:,} likes")

    normalized: dict[str, object] = {
        "source_model_id": source_model_id,
        "display_name": display_name,
        "license": license_str,
        "tags": tags,
        "pipeline_tag": pipeline_tag,
        "library_name": library_name,
        "model_type": model_type,
        "downloads": downloads,
        "likes": likes,
        "card_metadata": card_metadata,
    }

    # Compact note — never expose long card text
    note_parts: list[str] = []
    if license_str:
        note_parts.append(f"License: {license_str}")
    if pipeline_tag:
        note_parts.append(f"Task: {pipeline_tag}")
    if tags:
        top_tags = tags[:5]
        note_parts.append(f"Tags: {', '.join(top_tags)}")
    note_str = "; ".join(note_parts) if note_parts else "Hugging Face model metadata"

    return SourceModelRecord(
        source="huggingface",
        source_model_id=source_model_id,
        observed_at=now,
        raw_hash=raw_hash,
        raw_payload=raw,
        normalized=normalized,
        display_name=display_name,
        modalities=modalities,
        license=license_str,
        confidence=0.6,
        sparse=False,
        notes=(note_str,),
    )


# ---------------------------------------------------------------------------
# Field extraction helpers
# ---------------------------------------------------------------------------


def _opt_str(raw: dict[str, object], key: str) -> str | None:
    val = raw.get(key)
    return val if isinstance(val, str) and val else None


def _opt_int(raw: dict[str, object], key: str) -> int | None:
    val = raw.get(key)
    if isinstance(val, (int, float)) and val > 0:
        return int(val)
    return None


def _opt_list(raw: dict[str, object], key: str) -> list[str] | None:
    val = raw.get(key)
    if isinstance(val, list):
        items: list[object] = cast("list[object]", val)
        result: list[str] = []
        for item in items:
            if isinstance(item, str):
                result.append(item)
        return result if result else None
    return None
