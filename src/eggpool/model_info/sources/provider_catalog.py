"""Adapter that reads the in-memory ModelCatalogCache."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from eggpool.model_info.types import SourceModelRecord

if TYPE_CHECKING:
    from eggpool.catalog.cache import ModelCatalogCache

logger = logging.getLogger(__name__)


class ProviderCatalogSource:
    """Source that yields SourceModelRecords from the in-memory catalog cache."""

    name = "provider_catalog"
    priority = 10

    def __init__(self, cache: ModelCatalogCache) -> None:
        self._cache = cache

    async def fetch_all(self) -> list[SourceModelRecord]:
        records: list[SourceModelRecord] = []
        for (model_id, provider_id), entry in self._cache._provider_models.items():  # pyright: ignore[reportPrivateUsage]
            record = self._build_record(model_id, provider_id, entry)
            records.append(record)
        return records

    async def fetch_one(
        self, model_id: str, *, provider_id: str | None = None
    ) -> SourceModelRecord | None:
        if provider_id is not None:
            entry = self._cache._provider_models.get((model_id, provider_id))  # pyright: ignore[reportPrivateUsage]
            if entry is None:
                return None
            return self._build_record(model_id, provider_id, entry)
        for (mid, pid), entry in self._cache._provider_models.items():  # pyright: ignore[reportPrivateUsage]
            if mid == model_id:
                return self._build_record(mid, pid, entry)
        return None

    def _build_record(
        self,
        model_id: str,
        provider_id: str,
        entry: dict[str, object],
    ) -> SourceModelRecord:
        raw_hash = hashlib.sha256(
            json.dumps(entry, sort_keys=True, default=str).encode()
        ).hexdigest()

        last_seen = entry.get("last_seen_at")
        if isinstance(last_seen, (int, float)):
            observed_at = datetime.fromtimestamp(last_seen, tz=UTC)
        else:
            observed_at = datetime.now(UTC)

        limits = self._cache._effective_limits_from_info(entry)  # pyright: ignore[reportPrivateUsage]

        caps_raw = entry.get("capabilities")
        caps = cast("dict[str, object]", caps_raw) if isinstance(caps_raw, dict) else {}
        thinking_cap = (
            caps.get("thinking") if isinstance(caps.get("thinking"), dict) else None
        )

        modalities: frozenset[str] = frozenset({"text"})
        if caps.get("supports_vision"):
            modalities = modalities | {"vision"}

        display_name = entry.get("display_name")
        if isinstance(display_name, str):
            display_name_str: str | None = display_name
        else:
            display_name_str = None

        aliases: tuple[str, ...] = ()
        if display_name_str is not None and display_name_str != model_id:
            aliases = (model_id, display_name_str)

        return SourceModelRecord(
            source="provider_catalog",
            source_model_id=model_id,
            model_id=model_id,
            provider_id=provider_id,
            observed_at=observed_at,
            raw_hash=raw_hash,
            raw_payload=entry,
            normalized={},
            display_name=display_name_str,
            context_window=limits.context_tokens if limits else None,
            max_input_tokens=limits.input_tokens if limits else None,
            max_output_tokens=limits.output_tokens if limits else None,
            modalities=modalities,
            supports_tools=(
                bool(caps.get("supports_tools")) if "supports_tools" in caps else None
            ),
            thinking_capability=dict(cast("dict[str, object]", thinking_cap))
            if isinstance(thinking_cap, dict)
            else None,
            confidence=1.0,
            sparse=True,
            aliases=aliases,
        )
