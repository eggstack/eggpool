"""OpenRouter model-info source adapter.

Fetches the OpenRouter ``/models`` catalog and emits ``SourceModelRecord``
observations for each entry.  The catalog is naturally bulk; the adapter
fetches once per TTL window and indexes by source model ID in memory.

Design constraints (from the phase-3 plan):

- Does **not** replace the existing pricing resolver.
- Does **not** add models to the routable catalog.
- Exact / curated alias matching only; no fuzzy matching.
- Failures are recorded as source-health errors and do not break
  startup, catalog refresh, or routing.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

import httpx

from eggpool.errors import ModelInfoSourceFetchError
from eggpool.model_info.sources.base import SourceTTLCache
from eggpool.model_info.types import SourceModelRecord

if TYPE_CHECKING:
    from eggpool.models.config import ModelInfoSourceConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP client protocol (same shape as the pricing catalog client)
# ---------------------------------------------------------------------------


class ModelInfoHttpClient(Protocol):
    """Minimal async HTTP client used by model-info sources."""

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> httpx.Response: ...


_OpenRouterTTLCache = SourceTTLCache


# ---------------------------------------------------------------------------
# OpenRouter model-info source
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenRouterModelInfoEntry:
    """Parsed OpenRouter model entry."""

    source_model_id: str
    display_name: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    modalities: frozenset[str] = frozenset()
    supports_tools: bool | None = None
    supports_reasoning: bool | None = None
    input_price_per_1k: float | None = None
    output_price_per_1k: float | None = None
    created_at: datetime | None = None
    raw: dict[str, object] = field(default_factory=dict[str, object])


class OpenRouterModelInfoSource:
    """OpenRouter ``/models`` endpoint as a model-info observation source."""

    name = "openrouter"

    def __init__(
        self,
        *,
        config: ModelInfoSourceConfig,
        client: ModelInfoHttpClient,
        cache: _OpenRouterTTLCache | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._cache = cache or _OpenRouterTTLCache(
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
        base = self._config.base_url or "https://openrouter.ai/api/v1"
        return f"{base.rstrip('/')}/models"

    async def fetch_all(self) -> list[SourceModelRecord]:
        """Fetch the full OpenRouter catalog and return ``SourceModelRecord``s."""
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
                    f"OpenRouter model-info fetch failed: {exc}"
                ) from exc
            entries = _parse_catalog_payload(payload_obj)
            self._cache.store(entries)
            return entries


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_catalog_payload(payload: object) -> dict[str, dict[str, object]]:
    """Parse the OpenRouter /models response into a dict keyed by model ID."""
    entries: dict[str, dict[str, object]] = {}
    if not isinstance(payload, dict):
        return entries
    data_dict: dict[str, Any] = cast("dict[str, Any]", payload)
    data_obj: object = data_dict.get("data", [])
    if not isinstance(data_obj, list):
        return entries
    for raw_obj in cast("list[object]", data_obj):
        if not isinstance(raw_obj, dict):
            continue
        raw_dict: dict[str, Any] = cast("dict[str, Any]", raw_obj)
        model_id_obj: object = raw_dict.get("id")
        if not isinstance(model_id_obj, str) or not model_id_obj:
            continue
        entries[model_id_obj] = raw_dict
    return entries


def _parse_entry_to_record(
    source_model_id: str,
    raw: dict[str, object],
    now: datetime,
) -> SourceModelRecord:
    """Convert a raw OpenRouter model dict into a ``SourceModelRecord``."""
    raw_hash = hashlib.sha256(
        json.dumps(raw, sort_keys=True, default=str).encode()
    ).hexdigest()

    # Display name: name > title > id
    display_name = _opt_str(raw, "name") or _opt_str(raw, "title") or source_model_id

    # Context window
    context_window = (
        _opt_int(raw, "context_length")
        or _opt_int(raw, "context_window")
        or _opt_int(raw, "max_context_tokens")
    )

    # Max output: top_provider.max_completion_tokens > max_completion_tokens
    # > max_output_tokens
    max_output = _nested_int(raw, "top_provider", "max_completion_tokens")
    if max_output is None:
        max_output = _opt_int(raw, "max_completion_tokens")
    if max_output is None:
        max_output = _opt_int(raw, "max_output_tokens")

    # Modalities from architecture fields
    modalities = _parse_modalities(raw)

    # Tool / reasoning support
    supports_tools = _opt_bool(raw, "supported_parameters", _has_tool_value)
    supports_reasoning = _opt_bool(raw, "supported_parameters", _has_reasoning_value)
    thinking_capability = _extract_thinking_capability(raw)

    # Pricing (advisory, not cost-calculation truth)
    pricing_raw: object = raw.get("pricing") or {}
    pricing: dict[str, object] = (
        cast("dict[str, object]", pricing_raw) if isinstance(pricing_raw, dict) else {}
    )
    input_price = _safe_parse_price(pricing, "prompt", source_model_id)
    output_price = _safe_parse_price(pricing, "completion", source_model_id)

    # Created timestamp
    created_at = _opt_datetime(raw, "created")

    normalized: dict[str, object] = {
        "source_model_id": source_model_id,
        "display_name": display_name,
        "context_window": context_window,
        "max_output_tokens": max_output,
        "modalities": sorted(modalities),
        "supports_tools": supports_tools,
        "supports_reasoning": supports_reasoning,
        "input_price_per_1k": input_price,
        "output_price_per_1k": output_price,
        "created_at": created_at.isoformat() if created_at else None,
    }

    return SourceModelRecord(
        source="openrouter",
        source_model_id=source_model_id,
        observed_at=now,
        raw_hash=raw_hash,
        raw_payload=raw,
        normalized=normalized,
        display_name=display_name,
        context_window=context_window,
        max_output_tokens=max_output,
        modalities=modalities,
        supports_tools=supports_tools,
        supports_reasoning=supports_reasoning,
        thinking_capability=thinking_capability,
        input_price_per_1k=input_price,
        output_price_per_1k=output_price,
        confidence=0.5,
        sparse=not bool(display_name and display_name != source_model_id),
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


def _nested_int(raw: dict[str, object], outer_key: str, inner_key: str) -> int | None:
    outer = raw.get(outer_key)
    if isinstance(outer, dict):
        val = outer.get(inner_key)  # type: ignore[union-attr]
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
    return None


def _opt_bool(
    raw: dict[str, object],
    key: str,
    predicate: Any,
) -> bool | None:
    """Check if a list-valued field contains a specific value."""
    val = raw.get(key)
    if isinstance(val, list):
        str_items = [str(item) for item in cast("list[object]", val)]
        return bool(predicate(str_items))
    return None


def _has_tool_value(items: list[str]) -> bool:
    return any("tool" in item.lower() for item in items)


def _has_reasoning_value(items: list[str]) -> bool:
    return any(
        "reasoning" in item.lower() or "thinking" in item.lower() for item in items
    )


def _extract_thinking_capability(raw: dict[str, object]) -> dict[str, object] | None:
    """Extract explicit thinking/reasoning API-control capability.

    Only returns a capability dict when the source explicitly documents
    API-control support (e.g. via supported_parameters listing reasoning
    or thinking). Vague descriptions like "reasoning model" are NOT
    sufficient.
    """
    params = raw.get("supported_parameters")
    if not isinstance(params, list):
        return None
    str_params = [str(p).lower() for p in cast("list[object]", params)]
    has_reasoning = any("reasoning" in p or "thinking" in p for p in str_params)
    if not has_reasoning:
        return None
    return {
        "status": "supported",
        "source": "model_info",
        "confidence": "high",
        "notes": "OpenRouter reports reasoning/thinking in supported_parameters",
    }


def _parse_modalities(raw: dict[str, object]) -> frozenset[str]:
    """Parse modalities from architecture fields."""
    modalities: set[str] = set()

    arch = raw.get("architecture")
    if isinstance(arch, dict):
        arch_dict: dict[str, Any] = cast("dict[str, Any]", arch)
        input_mods = arch_dict.get("input_modalities")
        if isinstance(input_mods, list):
            for mod in cast("list[object]", input_mods):
                if isinstance(mod, str):
                    modalities.add(mod.lower())
        output_mods = arch_dict.get("output_modalities")
        if isinstance(output_mods, list):
            for mod in cast("list[object]", output_mods):
                if isinstance(mod, str):
                    modalities.add(mod.lower())

    if not modalities:
        modalities.add("text")

    return frozenset(modalities)


def _safe_parse_price(
    pricing: dict[str, object], key: str, source_model_id: str
) -> float | None:
    """Safely parse a pricing field, returning None on error."""
    from eggpool.catalog.pricing import parse_price_per_1k

    val = pricing.get(key)
    if val is None:
        return None
    try:
        return parse_price_per_1k(val, default_unit="token")
    except Exception:
        logger.debug(
            "Ignoring invalid OpenRouter price %s for %s: %r",
            key,
            source_model_id,
            val,
        )
        return None


def _opt_datetime(raw: dict[str, object], key: str) -> datetime | None:
    val = raw.get(key)
    if isinstance(val, (int, float)):
        try:
            return datetime.fromtimestamp(val, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
