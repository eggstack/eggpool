"""Base protocol for model-info observation sources."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from eggpool.model_info.types import SourceModelRecord


@runtime_checkable
class ModelInfoSource(Protocol):
    """Protocol for sources that provide model metadata observations."""

    name: str

    @property
    def priority(self) -> int: ...

    async def fetch_all(self) -> list[SourceModelRecord]: ...

    async def fetch_one(
        self, model_id: str, *, provider_id: str | None = None
    ) -> SourceModelRecord | None: ...


class SourceTTLCache:
    """Small bounded TTL cache indexed by source model ID."""

    def __init__(self, ttl_seconds: int, max_entries: int = 4096) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._data: OrderedDict[str, dict[str, object]] = OrderedDict()
        self._fetched_at: float = 0.0
        self._lock: asyncio.Lock = asyncio.Lock()

    @property
    def lock(self) -> asyncio.Lock:
        """Public accessor for the fetch lock."""
        return self._lock

    @property
    def is_fresh(self) -> bool:
        if self._fetched_at == 0.0:
            return False
        return (time.monotonic() - self._fetched_at) < self._ttl

    def invalidate(self) -> None:
        self._data = OrderedDict()
        self._fetched_at = 0.0

    def store(self, entries: dict[str, dict[str, object]]) -> None:
        """Replace the cached dataset with ``entries``."""
        self._data = OrderedDict(entries)
        self._fetched_at = time.monotonic()
        self._evict_to_capacity()

    def upsert(self, key: str, entry: dict[str, object]) -> None:
        """Insert or update one entry without evicting unrelated records."""
        if key in self._data:
            del self._data[key]
        self._data[key] = entry
        self._fetched_at = time.monotonic()
        self._evict_to_capacity()

    def _evict_to_capacity(self) -> None:
        while len(self._data) > self._max_entries:
            self._data.popitem(last=False)

    def get(self, key: str) -> dict[str, object] | None:
        return self._data.get(key)

    def snapshot(self) -> dict[str, dict[str, object]]:
        return dict(self._data)
