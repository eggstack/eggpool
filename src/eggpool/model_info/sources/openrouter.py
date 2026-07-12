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
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

import httpx

from eggpool.errors import ModelInfoSourceFetchError
from eggpool.model_info.sources.base import SourceTTLCache
from eggpool.model_info.types import BenchmarkObservation, SourceModelRecord

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

    def _benchmarks_url(self) -> str:
        """Return the unified OpenRouter benchmark endpoint.

        OpenRouter moved benchmark rankings out of the models response for
        some catalog versions.  Keep the path configurable for compatible
        proxies and test doubles, while defaulting to the documented API.
        """
        base = self._config.base_url or "https://openrouter.ai/api/v1"
        path_obj = self._config.options.get("benchmarks_path", "/benchmarks")
        path = path_obj if isinstance(path_obj, str) and path_obj else "/benchmarks"
        return f"{base.rstrip('/')}/{path.lstrip('/')}"

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

    def invalidate_cache(self) -> None:
        """Drop the cached OpenRouter catalog.

        Used by the forced-refresh path when configured aliases exist
        but do not match the cached catalog — the operator deserves a
        fresh attempt rather than a stale ``alias_not_in_catalog``
        answer (Phase 2.4 of the OpenRouter enrichment plan).
        """
        self._cache.invalidate()

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
            if entries:
                # Benchmark rankings are a best-effort enrichment.  A
                # missing/unauthorized benchmark endpoint must not discard a
                # healthy model catalog or make all model-info unavailable.
                # The documented endpoint requires an OpenRouter API key.
                # The public /models response already carries benchmark
                # fields for many models, so avoid a guaranteed 401 on
                # installations that intentionally configure no OpenRouter
                # key.  Operators can still opt in for older catalog shapes
                # through the explicit compatibility option.
                fetch_benchmarks_without_key = bool(
                    self._config.options.get("fetch_benchmarks_without_api_key", False)
                )
                if self._config.resolved_api_key or fetch_benchmarks_without_key:
                    benchmark_payload = await self._fetch_benchmark_payload()
                    _merge_benchmark_catalog_into_entries(entries, benchmark_payload)
            else:
                # Never make a malformed/empty response fresh for the whole
                # TTL window.  A transient proxy response or an upstream
                # schema change must be retried on the next cycle.
                self._cache.invalidate()
                logger.warning(
                    "OpenRouter model-info fetch returned no catalog entries; "
                    "not caching the empty response"
                )
                return entries
            self._cache.store(entries)
            return entries

    async def _fetch_benchmark_payload(self) -> object | None:
        """Fetch unified rankings without making the model catalog fragile."""
        try:
            response = await self._client.get(
                self._benchmarks_url(), headers=self._headers()
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.info(
                "OpenRouter benchmark endpoint unavailable; preserving model "
                "metadata from /models: %s",
                exc,
            )
            return None


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


def _merge_benchmark_catalog_into_entries(
    entries: dict[str, dict[str, object]], payload: object | None
) -> None:
    """Merge ``/benchmarks`` rows into the corresponding model payloads.

    The endpoint returns one row per model/source.  The models endpoint uses
    a nested object, so normalize both current and older response shapes into
    that object before the existing benchmark parser runs.
    """
    if not isinstance(payload, dict):
        return
    payload_dict = cast("dict[str, object]", payload)
    data_obj = payload_dict.get("data")
    if not isinstance(data_obj, list):
        return

    for item_obj in cast("list[object]", data_obj):
        if not isinstance(item_obj, dict):
            continue
        item = cast("dict[str, object]", item_obj)
        model_id = _benchmark_model_id(item)
        resolved_model_id = _resolve_benchmark_entry_id(model_id, entries)
        if resolved_model_id is None:
            continue

        raw = dict(entries[resolved_model_id])
        existing_obj = raw.get("benchmarks")
        benchmark_block: dict[str, object] = (
            dict(cast("dict[str, object]", existing_obj))
            if isinstance(existing_obj, dict)
            else {}
        )
        source = str(item.get("source", "")).strip().lower().replace("-", "_")

        evaluations = item.get("evaluations")
        has_aa_keys = any(
            token in str(key).lower()
            for key in item
            for token in ("intelligence", "coding", "agentic", "math", "quality")
        )
        if (
            source in {"artificial_analysis", "aa"}
            or (source == "" and has_aa_keys)
            or (
                isinstance(evaluations, dict)
                and any(
                    "intelligence" in str(key).lower()
                    or "coding" in str(key).lower()
                    or "agentic" in str(key).lower()
                    for key in cast("dict[object, object]", evaluations)
                )
            )
        ):
            aa_obj = benchmark_block.get("artificial_analysis")
            aa: dict[str, object] = (
                dict(cast("dict[str, object]", aa_obj))
                if isinstance(aa_obj, dict)
                else {}
            )
            if isinstance(evaluations, dict):
                for key, value in cast("dict[object, object]", evaluations).items():
                    if isinstance(key, str) and _numeric_value(value) is not None:
                        aa[key.removeprefix("artificial_analysis_")] = value
            for key, value in item.items():
                if key in {
                    "id",
                    "model_id",
                    "model_permaslug",
                    "model",
                    "slug",
                    "display_name",
                    "source",
                    "evaluations",
                    "pricing",
                }:
                    continue
                if _numeric_value(value) is not None:
                    aa[key.removeprefix("artificial_analysis_")] = value
            if aa:
                benchmark_block["artificial_analysis"] = aa
        elif source == "design_arena":
            design_obj = benchmark_block.get("design_arena")
            design_rows: list[object] = (
                list(cast("list[object]", design_obj))
                if isinstance(design_obj, list)
                else []
            )
            design_row = {
                key: item[key]
                for key in ("arena", "category", "elo", "win_rate", "rank")
                if key in item
            }
            if design_row:
                design_rows.append(design_row)
                benchmark_block["design_arena"] = design_rows
        else:
            # Preserve unfamiliar benchmark providers instead of throwing
            # away useful numeric results.  The generic parser below renders
            # these rows with OpenRouter provenance.
            generic_obj = benchmark_block.get("other")
            generic_rows: list[object] = (
                list(cast("list[object]", generic_obj))
                if isinstance(generic_obj, list)
                else []
            )
            generic_rows.append(dict(item))
            benchmark_block["other"] = generic_rows

        if benchmark_block:
            raw["benchmarks"] = benchmark_block
            entries[resolved_model_id] = raw


def _benchmark_model_id(item: dict[str, object]) -> str | None:
    """Extract a model id from current and legacy benchmark row shapes."""
    for key in ("model_permaslug", "model_id", "id", "slug"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    model_obj = item.get("model")
    if isinstance(model_obj, dict):
        model = cast("dict[str, object]", model_obj)
        for key in ("id", "slug", "model_permaslug"):
            value = model.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _resolve_benchmark_entry_id(
    benchmark_model_id: str | None,
    entries: dict[str, dict[str, object]],
) -> str | None:
    """Resolve a benchmark row to one model-catalog entry.

    OpenRouter has used several identifiers for the same model across
    benchmark and model endpoints: ``model_permaslug``/``model_id``,
    ``canonical_slug``, and provider variants such as ``:free``.  Prefer
    exact identity, then a unique base-variant match.  Ambiguous variants
    are rejected rather than attaching a score to the wrong model.
    """
    if not isinstance(benchmark_model_id, str) or not benchmark_model_id:
        return None
    if benchmark_model_id in entries:
        return benchmark_model_id

    candidates: list[str] = []
    benchmark_base = benchmark_model_id.removesuffix(":free")
    for entry_id, raw in entries.items():
        aliases = {entry_id}
        canonical_slug = raw.get("canonical_slug")
        if isinstance(canonical_slug, str) and canonical_slug:
            aliases.add(canonical_slug)
        for alias in aliases:
            if alias == benchmark_model_id:
                return entry_id
            if alias.removesuffix(":free") == benchmark_base:
                candidates.append(entry_id)
                break

    unique_candidates = sorted(set(candidates))
    return unique_candidates[0] if len(unique_candidates) == 1 else None


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

    # OpenRouter exposes public benchmark metadata under a nested
    # ``benchmarks`` object.  Keep it in the normalized observation as
    # well as the typed record so it survives the DB round-trip.
    benchmarks = _parse_benchmarks(raw, now)

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
        "benchmarks": [_benchmark_to_payload(b) for b in benchmarks],
    }

    aliases: list[str] = []
    canonical_slug = raw.get("canonical_slug")
    if isinstance(canonical_slug, str) and canonical_slug:
        aliases.append(canonical_slug)

    return SourceModelRecord(
        source="openrouter",
        source_model_id=source_model_id,
        observed_at=now,
        raw_hash=raw_hash,
        raw_payload=raw,
        normalized=normalized,
        aliases=tuple(sorted(set(aliases))),
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
        benchmarks=benchmarks,
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


def _parse_benchmarks(
    raw: dict[str, object],
    observed_at: datetime,
) -> tuple[BenchmarkObservation, ...]:
    """Parse OpenRouter's nested public benchmark metadata.

    The current OpenRouter catalog shape is, for example::

        {"benchmarks": {"artificial_analysis": {
            "intelligence_index": 55.7,
            "coding_index": 74.3,
        }}}

    ``design_arena`` is also published as a list of per-category rows.  The
    parser intentionally ignores malformed or non-numeric values so one
    provider's catalog variation cannot make the whole source unavailable.
    """
    raw_benchmarks_obj = raw.get("benchmarks")
    if isinstance(raw_benchmarks_obj, list):
        raw_benchmarks: dict[str, object] = {"other": raw_benchmarks_obj}
    elif isinstance(raw_benchmarks_obj, dict):
        raw_benchmarks = cast("dict[str, object]", raw_benchmarks_obj)
    else:
        return ()

    benchmarks: list[BenchmarkObservation] = []

    artificial_analysis_obj = raw_benchmarks.get("artificial_analysis")
    if isinstance(artificial_analysis_obj, dict):
        artificial_analysis = cast("dict[str, object]", artificial_analysis_obj)
        for key, value in artificial_analysis.items():
            score = _numeric_value(value)
            if score is None:
                continue
            label = _benchmark_label(str(key))
            if not label:
                continue
            benchmarks.append(
                BenchmarkObservation(
                    benchmark_name=f"Artificial Analysis {label}",
                    score=score,
                    source="artificial_analysis",
                    observed_at=observed_at,
                    notes="Published by OpenRouter",
                )
            )

    design_arena_obj = raw_benchmarks.get("design_arena")
    if isinstance(design_arena_obj, list):
        design_arena = cast("list[object]", design_arena_obj)
        for item in design_arena:
            if not isinstance(item, dict):
                continue
            item_dict = cast("dict[str, object]", item)
            arena = item_dict.get("arena")
            category = item_dict.get("category")
            name_parts = [
                str(part).strip()
                for part in (arena, category)
                if isinstance(part, str) and part.strip()
            ]
            name = " / ".join(name_parts)
            if not name:
                continue
            elo = item_dict.get("elo")
            win_rate = item_dict.get("win_rate")
            elo_score = _numeric_value(elo)
            win_rate_score = _numeric_value(win_rate)
            score_value = elo_score if elo_score is not None else win_rate_score
            if score_value is None:
                continue
            rank_value = item_dict.get("rank")
            rank_number = _numeric_value(rank_value)
            rank = int(rank_number) if rank_number is not None else None
            notes = (
                f"Win rate: {win_rate}"
                if win_rate_score is not None
                else "Published by OpenRouter"
            )
            benchmarks.append(
                BenchmarkObservation(
                    benchmark_name=f"Design Arena: {name}",
                    score=score_value,
                    rank=rank,
                    source="openrouter",
                    observed_at=observed_at,
                    notes=notes,
                )
            )

    # Be forward-compatible with additional benchmark providers and with
    # older clients that represented benchmark rows as a list.
    for source_key, source_obj in raw_benchmarks.items():
        if source_key in {"artificial_analysis", "design_arena"}:
            continue
        if isinstance(source_obj, dict):
            source_map = cast("dict[str, object]", source_obj)
            for key, value in source_map.items():
                score = _numeric_value(value)
                if score is None:
                    continue
                label = _benchmark_label(str(key))
                if label:
                    benchmarks.append(
                        BenchmarkObservation(
                            benchmark_name=f"{_benchmark_label(source_key)} {label}",
                            score=score,
                            source="openrouter",
                            observed_at=observed_at,
                            notes="Published by OpenRouter",
                        )
                    )
        elif isinstance(source_obj, list):
            for item_obj in cast("list[object]", source_obj):
                if not isinstance(item_obj, dict):
                    continue
                item = cast("dict[str, object]", item_obj)
                name_obj = item.get("name", item.get("benchmark"))
                name = str(name_obj).strip() if isinstance(name_obj, str) else ""
                score = _numeric_value(
                    item.get("score", item.get("elo", item.get("win_rate")))
                )
                if not name or score is None:
                    continue
                rank_value = _numeric_value(item.get("rank"))
                benchmarks.append(
                    BenchmarkObservation(
                        benchmark_name=name,
                        score=score,
                        rank=int(rank_value) if rank_value is not None else None,
                        source="openrouter",
                        observed_at=observed_at,
                        notes="Published by OpenRouter",
                    )
                )

    return tuple(benchmarks)


def _benchmark_label(key: str) -> str:
    """Turn a wire benchmark key into a stable human-readable label."""
    normalized = key.removeprefix("artificial_analysis_").replace("_", " ")
    return " ".join(part.capitalize() for part in normalized.split())


def _numeric_value(value: object) -> float | None:
    """Return a real numeric value, excluding booleans."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if math.isfinite(numeric):
            return numeric
    if isinstance(value, str):
        try:
            numeric = float(value.strip())
        except ValueError:
            return None
        if math.isfinite(numeric):
            return numeric
    return None


def _benchmark_to_payload(benchmark: BenchmarkObservation) -> dict[str, object]:
    """Serialize a typed benchmark observation for normalized persistence."""
    return {
        "name": benchmark.benchmark_name,
        "score": benchmark.score,
        "rank": benchmark.rank,
        "percentile": benchmark.percentile,
        "version": benchmark.version,
        "source": benchmark.source,
        "observed_at": (
            benchmark.observed_at.isoformat() if benchmark.observed_at else None
        ),
        "notes": benchmark.notes,
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
