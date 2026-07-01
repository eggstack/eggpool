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

        # Build the alias set in deterministic order. Identity resolution
        # rules require exact alias matches — no fuzzy or substring
        # matching. External model-info sources (OpenRouter, Artificial
        # Analysis, Hugging Face) all use a ``<vendor>/<model>`` source
        # ID format where ``<vendor>`` is the Eggpool ``provider_id``;
        # seeding that canonical alias here makes the OpenRouter resolver
        # match every catalog row that has a configured provider, with
        # zero operator configuration.
        alias_set: set[str] = set()
        if provider_id and provider_id != model_id:
            prefixed = f"{provider_id}/{model_id}"
            alias_set.add(prefixed)
        if display_name_str is not None and display_name_str != model_id:
            alias_set.add(display_name_str)
        # Always keep the bare model id as a self-alias so future sources
        # that happen to use the same identifier resolve without
        # requiring the caller to set it explicitly.
        alias_set.add(model_id)
        aliases: tuple[str, ...] = tuple(sorted(alias_set))

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
