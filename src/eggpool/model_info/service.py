"""Service skeleton for model-info subsystem."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from eggpool.errors import ModelInfoSourceFetchError
from eggpool.model_info.dedup import canonical_needs_update
from eggpool.model_info.identity import resolve_openrouter_record
from eggpool.model_info.repository import ModelInfoRepository
from eggpool.model_info.scheduler import ModelInfoRefreshScheduler
from eggpool.model_info.sources.provider_catalog import ProviderCatalogSource
from eggpool.model_info.types import (
    CanonicalModelInfo,
    ModelInfoStatus,
    SourceModelRecord,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from eggpool.catalog.cache import ModelCatalogCache
    from eggpool.catalog.service import CatalogRefreshResult
    from eggpool.db.connection import Database
    from eggpool.model_info.sources.openrouter import ModelInfoHttpClient
    from eggpool.models.config import ModelInfoConfig

logger = logging.getLogger(__name__)


class ModelInfoService:
    """Orchestrates model-info loading, reconciliation, and summary generation."""

    def __init__(
        self,
        config: ModelInfoConfig,
        db: Database,
        catalog: ModelCatalogCache,
        *,
        outbound_client: ModelInfoHttpClient | None = None,
    ) -> None:
        self._config = config
        self._db = db
        self._catalog = catalog
        self._repo = ModelInfoRepository(db)
        self._provider_source = ProviderCatalogSource(catalog)
        self._scheduler = ModelInfoRefreshScheduler(config)

        # External sources (optional)
        self._openrouter_source: OpenRouterModelInfoSource | None = None
        if config.sources.openrouter.enabled and outbound_client is not None:
            from eggpool.model_info.sources.openrouter import (
                OpenRouterModelInfoSource,
            )

            self._openrouter_source = OpenRouterModelInfoSource(
                config=config.sources.openrouter,
                client=outbound_client,
            )

    @property
    def repo(self) -> ModelInfoRepository:
        return self._repo

    async def load_cache(self) -> None:
        """Load provider-native observations into the DB from the catalog cache."""
        if not self._config.enabled:
            return
        await self.refresh_provider_catalog_observations()

    async def refresh_provider_catalog_observations(self) -> dict[str, int]:
        """Fetch all provider-catalog observations and upsert them.

        Returns a summary dict with counts.
        """
        records = await self._provider_source.fetch_all()
        upserted = 0
        aliases_created = 0
        for record in records:
            model_id = record.model_id or record.source_model_id
            provider_id = record.provider_id

            await self._repo.upsert_observation(
                record,
                model_id=model_id,
                provider_id=provider_id,
            )
            upserted += 1

            for alias in record.aliases:
                if alias != model_id:
                    await self._repo.upsert_alias(
                        model_id=model_id,
                        provider_id=provider_id,
                        alias=alias,
                        source=record.source,
                        confidence=record.confidence,
                    )
                    aliases_created += 1

        await self._repo.record_source_success("provider_catalog")
        return {"observations": upserted, "aliases": aliases_created}

    async def reconcile_catalog_snapshot(
        self, *, reason: str = "manual"
    ) -> dict[str, int]:
        """Reconcile catalog models with model-info canonical rows.

        For every model in the catalog, ensure a canonical row exists with
        the correct status and a deterministic summary.
        """
        model_ids = set(self._catalog._models.keys())  # pyright: ignore[reportPrivateUsage]

        created = 0
        updated = 0

        for model_id in model_ids:
            existing = await self._repo.get_canonical(model_id)
            now = datetime.now(UTC)

            status, sparse = self._classify_model(model_id)

            next_refresh = self._compute_next_refresh(status, now)

            detail = self._build_detail(model_id)
            provenance: dict[str, object] = {
                "sources": ["provider_catalog"],
                "reconciled_at": now.isoformat(),
            }
            conflicts: dict[str, object] = {}

            summary = _generate_summary(
                model_id=model_id,
                status=status,
                sparse=sparse,
                detail=detail,
            )

            if existing is None:
                info = CanonicalModelInfo(
                    model_id=model_id,
                    status=status,
                    summary=summary,
                    sparse=sparse,
                    detail=detail,
                    provenance=provenance,
                    conflicts=conflicts,
                    first_seen_at=now,
                    last_seen_at=now,
                    last_refreshed_at=now,
                    next_refresh_at=next_refresh,
                )
                await self._repo.upsert_canonical(info)
                created += 1
            else:
                info = CanonicalModelInfo(
                    model_id=model_id,
                    status=status,
                    summary=summary,
                    sparse=sparse,
                    detail=detail,
                    provenance={**existing.provenance, **provenance},
                    conflicts=conflicts,
                    first_seen_at=existing.first_seen_at,
                    last_seen_at=now,
                    last_refreshed_at=now,
                    next_refresh_at=next_refresh,
                )
                await self._repo.upsert_canonical(info)
                updated += 1

        return {"created": created, "updated": updated, "total": len(model_ids)}

    async def reconcile_catalog_refresh(
        self, result: CatalogRefreshResult
    ) -> dict[str, int]:
        """Reconcile model-info after a catalog refresh.

        For new models: create canonical row if absent, mark sparse_new,
        set next_refresh_at=now.
        For changed provider keys: refresh observations for affected models.
        For withdrawn models: set status 'withdrawn' if not live.

        Skips writes when the computed payload is byte-identical to the
        existing row (write-amplification avoidance for SBC deployments).
        Batch-writes all changes in a single transaction.
        """
        now = datetime.now(UTC)
        created = 0
        updated = 0
        refreshed = 0
        skipped = 0
        to_write: list[CanonicalModelInfo] = []

        # New models: create sparse canonical rows
        for model_id in result.new_model_ids:
            existing = await self._repo.get_canonical(model_id)
            if existing is None:
                status, sparse = self._classify_model(model_id)
                detail = self._build_detail(model_id)
                provenance: dict[str, object] = {
                    "sources": ["provider_catalog"],
                    "reconciled_at": now.isoformat(),
                }
                summary = _generate_summary(
                    model_id=model_id,
                    status=status,
                    sparse=sparse,
                    detail=detail,
                )
                info = CanonicalModelInfo(
                    model_id=model_id,
                    status=status,
                    summary=summary,
                    sparse=sparse,
                    detail=detail,
                    provenance=provenance,
                    conflicts={},
                    first_seen_at=now,
                    last_seen_at=now,
                    last_refreshed_at=None,
                    next_refresh_at=now,
                )
                to_write.append(info)
                created += 1

        # Changed provider keys: refresh observations for affected model IDs
        changed_model_ids = {
            model_id for model_id, _provider_id in result.changed_provider_keys
        }
        for model_id in changed_model_ids:
            if model_id in result.new_model_ids:
                continue  # already handled above
            existing = await self._repo.get_canonical(model_id)
            if existing is not None:
                detail = self._build_detail(model_id)
                provenance = {
                    **existing.provenance,
                    "sources": ["provider_catalog"],
                    "reconciled_at": now.isoformat(),
                }
                info = CanonicalModelInfo(
                    model_id=model_id,
                    status=existing.status,
                    summary=existing.summary,
                    sparse=existing.sparse,
                    detail=detail,
                    provenance=provenance,
                    conflicts=existing.conflicts,
                    first_seen_at=existing.first_seen_at,
                    last_seen_at=now,
                    last_refreshed_at=existing.last_refreshed_at,
                    next_refresh_at=existing.next_refresh_at,
                )
                if canonical_needs_update(existing, info):
                    to_write.append(info)
                    refreshed += 1
                else:
                    skipped += 1

        # Withdrawn models: mark withdrawn if not live in catalog
        for model_id in result.withdrawn_model_ids:
            existing = await self._repo.get_canonical(model_id)
            if existing is not None and model_id not in result.live_model_ids:
                info = CanonicalModelInfo(
                    model_id=model_id,
                    status=cast("ModelInfoStatus", "withdrawn"),
                    summary=existing.summary,
                    sparse=False,
                    detail=existing.detail,
                    provenance=existing.provenance,
                    conflicts=existing.conflicts,
                    first_seen_at=existing.first_seen_at,
                    last_seen_at=existing.last_seen_at,
                    last_refreshed_at=existing.last_refreshed_at,
                    next_refresh_at=None,
                )
                if canonical_needs_update(existing, info):
                    to_write.append(info)
                    updated += 1
                else:
                    skipped += 1

        if to_write:
            await self._repo.upsert_canonical_batch(to_write)

        return {
            "created": created,
            "updated": updated,
            "refreshed": refreshed,
            "skipped": skipped,
            "total": len(result.live_model_ids),
        }

    async def refresh_due_models(self) -> dict[str, int]:
        """Refresh provider-native and external observations for due models.

        Queries the repository for due rows, refreshes provider observations,
        attempts OpenRouter enrichment via identity resolution, reconciles
        canonical summaries, and updates next_refresh_at.
        Batch-writes all changes in a single transaction and skips rows
        where the computed payload is byte-identical to the existing row.
        """
        now = datetime.now(UTC)
        due_rows = await self._repo.list_due(
            limit=self._config.max_models_per_cycle, now=now
        )

        if not due_rows:
            return {"refreshed": 0, "total": 0, "skipped": 0}

        # Bulk-fetch OpenRouter catalog once per cycle if the source is active
        openrouter_indexed: dict[str, SourceModelRecord] = {}
        if self._openrouter_source is not None:
            try:
                or_records = await self._openrouter_source.fetch_all()
                openrouter_indexed = {r.source_model_id: r for r in or_records}
            except ModelInfoSourceFetchError as exc:
                logger.warning("OpenRouter source fetch failed: %s", exc)
                await self.record_source_error("openrouter", exc)
            except Exception as exc:
                logger.exception("OpenRouter source unexpected error")
                await self.record_source_error("openrouter", exc)

        to_write: list[CanonicalModelInfo] = []
        skipped = 0
        for canonical in due_rows:
            model_id = canonical.model_id
            existing = await self._repo.get_canonical(model_id)
            if existing is None:
                continue

            status, sparse = self._classify_model(model_id)
            detail = self._build_detail(model_id)
            next_refresh = self._scheduler.next_refresh_for(
                status=status,
                first_seen_at=existing.first_seen_at,
                last_refreshed_at=existing.last_refreshed_at,
                now=now,
            )

            # Try OpenRouter identity resolution for this model
            or_record = await resolve_openrouter_record(
                model_id, self._repo, openrouter_indexed
            )
            if or_record is not None:
                await self._persist_source_observation(or_record, model_id=model_id)
                await self.record_source_success("openrouter")

            # Enrich detail with OpenRouter fields when available
            or_detail = _enrich_detail_from_record(detail, or_record)
            conflicts = _detect_context_conflicts(detail, or_record, existing.conflicts)

            info = CanonicalModelInfo(
                model_id=model_id,
                status=status,
                summary=_generate_summary(
                    model_id=model_id,
                    status=status,
                    sparse=sparse,
                    detail=or_detail,
                ),
                sparse=sparse,
                detail=or_detail,
                provenance={
                    **existing.provenance,
                    "sources": _build_source_list(
                        existing.provenance, self._openrouter_source is not None
                    ),
                    "reconciled_at": now.isoformat(),
                },
                conflicts=conflicts,
                first_seen_at=existing.first_seen_at,
                last_seen_at=now,
                last_refreshed_at=now,
                next_refresh_at=next_refresh,
            )
            if canonical_needs_update(existing, info):
                to_write.append(info)
            else:
                skipped += 1

        if to_write:
            await self._repo.upsert_canonical_batch(to_write)

        return {
            "refreshed": len(to_write),
            "total": len(due_rows),
            "skipped": skipped,
        }

    async def _persist_source_observation(
        self,
        record: SourceModelRecord,
        *,
        model_id: str | None = None,
        provider_id: str | None = None,
    ) -> None:
        """Persist a source observation and its aliases.

        Respects ``store_raw_observations`` config:
        - When ``False``, stores ``{}`` in ``raw_json`` (normalized_json kept).
        - When ``True``, stores raw payload bounded to 64 KiB; entries
          exceeding the limit are replaced with a summary plus hash.
        """
        resolved_model_id = model_id or record.model_id or record.source_model_id
        resolved_provider_id = provider_id or record.provider_id

        # Optionally strip or bound raw payload before persisting
        if not self._config.store_raw_observations:
            record = _strip_raw_payload(record)
        else:
            record = _bound_raw_payload(record)

        await self._repo.upsert_observation(
            record,
            model_id=resolved_model_id,
            provider_id=resolved_provider_id,
        )
        for alias in record.aliases:
            if alias != resolved_model_id:
                await self._repo.upsert_alias(
                    model_id=resolved_model_id,
                    provider_id=resolved_provider_id,
                    alias=alias,
                    source=record.source,
                    confidence=record.confidence,
                )

    async def run_periodic_refresh(self) -> None:
        """Background loop that refreshes due models periodically."""
        while True:
            await asyncio.sleep(self._config.refresh_interval_s)
            try:
                result = await self.refresh_due_models()
                if result["refreshed"] > 0:
                    logger.info(
                        "Model info periodic refresh: refreshed %d of %d due models",
                        result["refreshed"],
                        result["total"],
                    )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Model info periodic refresh failed")

    async def record_source_success(self, source_name: str) -> None:
        """Record a successful fetch from a model-info source."""
        await self._repo.record_source_success(source_name)

    async def record_source_error(self, source_name: str, exc: Exception) -> None:
        """Record an error from a model-info source with exponential backoff."""
        failure_count = await self._repo.get_source_failure_count(source_name)
        cooldown = _compute_source_backoff(source_name, failure_count)
        await self._repo.record_source_error(source_name, exc, cooldown_until=cooldown)

    async def get_summary(self, model_id: str) -> CanonicalModelInfo | None:
        """Return the canonical summary for a model."""
        return await self._repo.get_canonical(model_id)

    async def get_summary_map(
        self, model_ids: Iterable[str] | None = None
    ) -> dict[str, CanonicalModelInfo]:
        """Return canonical summaries keyed by model ID."""
        if model_ids is not None:
            id_list = list(model_ids)
        else:
            id_list = list(self._catalog._models.keys())  # pyright: ignore[reportPrivateUsage]
        return await self._repo.get_canonical_many(id_list)

    def _classify_model(self, model_id: str) -> tuple[ModelInfoStatus, bool]:
        """Classify a model's info status based on available data.

        Returns (status, sparse) where sparse indicates the model has
        minimal provider-native metadata.

        Coverage fields computed (matching the plan):
        - has_provider_observation: at least one provider entry exists
        - has_display_name: a non-trivial display name is available
        - has_effective_context_or_upstream_context: context window known
        - has_capability_flags: tools or vision capabilities known
        - has_pricing_state: pricing metadata available (reserved)
        - has_family_or_release: model family or release date known (reserved)
        - has_benchmark_state: benchmark scores available (reserved)
        """
        provider_entries = {
            (mid, pid): entry
            for (mid, pid), entry in self._catalog._provider_models.items()  # pyright: ignore[reportPrivateUsage]
            if mid == model_id
        }

        has_provider_observation = bool(provider_entries)
        if not has_provider_observation:
            return (cast("ModelInfoStatus", "unmatched"), True)

        has_display_name = False
        has_context_limit = False
        has_tools_or_vision = False
        has_pricing_state = False
        has_family_or_release = False
        has_benchmark_state = False

        for _key, entry in provider_entries.items():
            display_name = entry.get("display_name")
            if (
                isinstance(display_name, str)
                and display_name
                and display_name != model_id
            ):
                has_display_name = True

            limits = self._catalog._effective_limits_from_info(entry)  # pyright: ignore[reportPrivateUsage]
            if limits and limits.context_tokens is not None:
                has_context_limit = True

            caps_raw = entry.get("capabilities")
            caps = (
                cast("dict[str, object]", caps_raw)
                if isinstance(caps_raw, dict)
                else {}
            )
            if (
                caps.get("supports_tools") is not None
                or caps.get("supports_vision") is not None
            ):
                has_tools_or_vision = True

            # Pricing: check if the global model entry has any pricing hints
            global_info = self._catalog._models.get(model_id)  # pyright: ignore[reportPrivateUsage]
            if global_info is not None:
                source_meta_raw = global_info.get("source_metadata")
                source_meta = (
                    cast("dict[str, object]", source_meta_raw)
                    if isinstance(source_meta_raw, dict)
                    else {}
                )
                if "pricing" in source_meta or "input_price" in source_meta:
                    has_pricing_state = True

            # Family/release: check if model_id contains a family hint
            # or if there's a source_metadata field indicating this
            if global_info is not None:
                source_meta_raw = global_info.get("source_metadata")
                source_meta = (
                    cast("dict[str, object]", source_meta_raw)
                    if isinstance(source_meta_raw, dict)
                    else {}
                )
                if "family" in source_meta or "release_date" in source_meta:
                    has_family_or_release = True

        indicators = [
            has_provider_observation,
            has_display_name,
            has_context_limit,
            has_tools_or_vision,
            has_pricing_state,
            has_family_or_release,
            has_benchmark_state,
        ]
        filled = sum(indicators)
        missing = len(indicators) - filled

        sparse = missing >= 4

        if sparse:
            return (cast("ModelInfoStatus", "sparse_new"), True)

        if filled <= 3:
            return (cast("ModelInfoStatus", "partial"), False)

        if not has_benchmark_state and not has_family_or_release:
            return (cast("ModelInfoStatus", "partial"), False)

        return (cast("ModelInfoStatus", "fresh"), False)

    def _compute_next_refresh(
        self, status: ModelInfoStatus, now: datetime
    ) -> datetime | None:
        """Compute the next refresh time based on status."""
        if status == "sparse_new":
            return now + timedelta(seconds=self._config.sparse_new_initial_ttl_s)
        if status == "partial":
            return now + timedelta(seconds=self._config.partial_ttl_s)
        if status == "fresh":
            return now + timedelta(seconds=self._config.known_ttl_s)
        if status == "conflicting":
            return now + timedelta(seconds=self._config.conflict_ttl_s)
        if status == "stale":
            return now + timedelta(seconds=self._config.refresh_interval_s)
        return now + timedelta(seconds=self._config.known_ttl_s)

    def _build_detail(self, model_id: str) -> dict[str, object]:
        """Build a detail dict from catalog data."""
        global_info = self._catalog._models.get(model_id)  # pyright: ignore[reportPrivateUsage]
        if global_info is None:
            return {}

        detail: dict[str, object] = {}

        display_name = global_info.get("display_name")
        if display_name:
            detail["display_name"] = display_name

        protocol = global_info.get("protocol")
        if protocol:
            detail["protocol"] = protocol

        caps_raw = global_info.get("capabilities")
        caps = cast("dict[str, object]", caps_raw) if isinstance(caps_raw, dict) else {}
        if caps.get("supports_tools") is not None:
            detail["supports_tools"] = caps["supports_tools"]
        if caps.get("supports_vision") is not None:
            detail["supports_vision"] = caps["supports_vision"]

        limits = self._catalog._effective_limits_from_info(global_info)  # pyright: ignore[reportPrivateUsage]
        if limits:
            if limits.context_tokens is not None:
                detail["context_tokens"] = limits.context_tokens
            if limits.input_tokens is not None:
                detail["input_tokens"] = limits.input_tokens
            if limits.output_tokens is not None:
                detail["output_tokens"] = limits.output_tokens

        providers = sorted(
            {
                pid
                for (mid, pid) in self._catalog._provider_models  # pyright: ignore[reportPrivateUsage]
                if mid == model_id
            }
        )
        if providers:
            detail["providers"] = providers

        return detail


def _enrich_detail_from_record(
    detail: dict[str, object],
    record: SourceModelRecord | None,
) -> dict[str, object]:
    """Enrich canonical detail with OpenRouter-sourced fields.

    OpenRouter fields are stored under explicit ``external_*`` keys so they
    never overwrite provider-native values.  The existing ``display_name``
    and ``context_tokens`` fields remain authoritative from the catalog.
    """
    if record is None:
        return dict(detail)

    enriched = dict(detail)

    # External IDs
    external_ids = dict(cast("dict[str, object]", enriched.get("external_ids", {})))
    external_ids["openrouter"] = record.source_model_id
    enriched["external_ids"] = external_ids

    # External context window (advisory only)
    if record.context_window is not None:
        enriched["context_window_external"] = record.context_window

    # External max output tokens (advisory only)
    if record.max_output_tokens is not None:
        enriched["max_output_tokens_external"] = record.max_output_tokens

    # External modalities (advisory only)
    if record.modalities:
        enriched["modalities_external"] = sorted(record.modalities)

    # Pricing observation (advisory, never cost-calculation truth)
    pricing_obs: dict[str, object] = {}
    if record.input_price_per_1k is not None:
        pricing_obs["input_price_per_1k"] = record.input_price_per_1k
    if record.output_price_per_1k is not None:
        pricing_obs["output_price_per_1k"] = record.output_price_per_1k
    if pricing_obs:
        enriched["pricing_observation"] = pricing_obs

    # Display name from OpenRouter (non-authoritative override)
    if record.display_name and record.display_name != record.source_model_id:
        enriched["display_name_external"] = record.display_name

    # Created timestamp (if present)
    normalized = record.normalized
    created_at = normalized.get("created_at")
    if created_at is not None:
        enriched["created_at_external"] = created_at

    return enriched


def _detect_context_conflicts(
    detail: dict[str, object],
    record: SourceModelRecord | None,
    existing_conflicts: dict[str, object],
) -> dict[str, object]:
    """Detect and record conflicts between provider-native and OpenRouter metadata.

    A conflict is recorded when both provider-local and OpenRouter values exist
    for context_window and differ materially (>10% relative difference).
    """
    conflicts = dict(existing_conflicts)

    if record is None:
        return conflicts

    local_ctx = detail.get("context_tokens")
    or_ctx = record.context_window

    if (
        isinstance(local_ctx, (int, float))
        and local_ctx > 0
        and or_ctx is not None
        and or_ctx > 0
    ):
        diff = abs(local_ctx - or_ctx)
        relative = diff / max(local_ctx, or_ctx)
        if relative > 0.10:
            conflicts["context_window"] = {
                "provider_catalog": local_ctx,
                "openrouter": or_ctx,
                "selected": "provider_catalog/effective_limit",
                "reason": "local/provider effective limit wins for Eggpool display",
            }

    return conflicts


def _build_source_list(
    provenance: dict[str, object], has_openrouter: bool
) -> list[str]:
    """Build the sources list for provenance, preserving existing entries."""
    existing = provenance.get("sources")
    sources: list[str] = []
    if isinstance(existing, list):
        for item in cast("list[object]", existing):
            if isinstance(item, str):
                sources.append(item)
    if has_openrouter and "openrouter" not in sources:
        sources.append("openrouter")
    if "provider_catalog" not in sources:
        sources.append("provider_catalog")
    return sources


def _compute_source_backoff(source_name: str, failure_count: int = 0) -> datetime:
    """Compute exponential backoff for source failures.

    Tiers: 15m → 1h → 6h → 24h cap.
    The ``source_name`` parameter is reserved for future per-source tuning.
    """
    if failure_count <= 0:
        cooldown_minutes = 15
    elif failure_count == 1:
        cooldown_minutes = 60
    elif failure_count == 2:
        cooldown_minutes = 360
    else:
        cooldown_minutes = 1440  # 24h cap
    return datetime.now(UTC) + timedelta(minutes=cooldown_minutes)


_RAW_PAYLOAD_BOUND_BYTES = 65_536  # 64 KiB


def _strip_raw_payload(record: SourceModelRecord) -> SourceModelRecord:
    """Return a copy of *record* with raw_payload replaced by ``{}``.

    Used when ``store_raw_observations`` is ``False``.
    """
    return SourceModelRecord(
        source=record.source,
        source_model_id=record.source_model_id,
        observed_at=record.observed_at,
        raw_hash=record.raw_hash,
        raw_payload={},
        normalized=record.normalized,
        aliases=record.aliases,
        provider_id=record.provider_id,
        model_id=record.model_id,
        display_name=record.display_name,
        family=record.family,
        context_window=record.context_window,
        max_input_tokens=record.max_input_tokens,
        max_output_tokens=record.max_output_tokens,
        modalities=record.modalities,
        supports_tools=record.supports_tools,
        supports_reasoning=record.supports_reasoning,
        input_price_per_1k=record.input_price_per_1k,
        output_price_per_1k=record.output_price_per_1k,
        benchmarks=record.benchmarks,
        release_date=record.release_date,
        license=record.license,
        confidence=record.confidence,
        sparse=record.sparse,
        notes=record.notes,
    )


def _bound_raw_payload(record: SourceModelRecord) -> SourceModelRecord:
    """Bound raw_payload size to ``_RAW_PAYLOAD_BOUND_BYTES``.

    If the serialised payload exceeds the limit, replace it with a summary
    dict containing selected fields and the original hash.
    """
    raw_json = json.dumps(record.raw_payload, sort_keys=True, default=str)
    if len(raw_json.encode()) <= _RAW_PAYLOAD_BOUND_BYTES:
        return record

    bounded: dict[str, object] = {
        "_summary": True,
        "source_model_id": record.source_model_id,
        "display_name": record.display_name,
        "raw_hash": record.raw_hash,
        "original_size_bytes": len(raw_json.encode()),
    }
    return SourceModelRecord(
        source=record.source,
        source_model_id=record.source_model_id,
        observed_at=record.observed_at,
        raw_hash=record.raw_hash,
        raw_payload=bounded,
        normalized=record.normalized,
        aliases=record.aliases,
        provider_id=record.provider_id,
        model_id=record.model_id,
        display_name=record.display_name,
        family=record.family,
        context_window=record.context_window,
        max_input_tokens=record.max_input_tokens,
        max_output_tokens=record.max_output_tokens,
        modalities=record.modalities,
        supports_tools=record.supports_tools,
        supports_reasoning=record.supports_reasoning,
        input_price_per_1k=record.input_price_per_1k,
        output_price_per_1k=record.output_price_per_1k,
        benchmarks=record.benchmarks,
        release_date=record.release_date,
        license=record.license,
        confidence=record.confidence,
        sparse=record.sparse,
        notes=record.notes,
    )


def _generate_summary(
    *,
    model_id: str,
    status: ModelInfoStatus,
    sparse: bool,
    detail: dict[str, object],
) -> str:
    """Generate a deterministic summary string for a model."""
    if status == "conflicting":
        return "Metadata conflict detected. Manual review recommended."

    parts: list[str] = []

    if sparse:
        parts.append("New model detected; metadata sparse.")

    providers = detail.get("providers")
    if isinstance(providers, list) and providers:
        provider_str = ", ".join(str(p) for p in cast("list[object]", providers))
        parts.append(f"Callable via {provider_str}.")
    else:
        parts.append("Provider information unavailable.")

    ctx = detail.get("context_tokens")
    if isinstance(ctx, (int, float)) and ctx > 0:
        if ctx >= 1_000_000:
            parts.append(f"Context window: {ctx / 1_000_000:.0f}M tokens.")
        elif ctx >= 1_000:
            parts.append(f"Context window: {ctx / 1_000:.0f}k tokens.")
        else:
            parts.append(f"Context window: {int(ctx)} tokens.")

    caps_parts: list[str] = []
    if detail.get("supports_tools") is True:
        caps_parts.append("tool support")
    if detail.get("supports_vision") is True:
        caps_parts.append("vision")
    if caps_parts:
        parts.append(f"Capabilities: {', '.join(caps_parts)}.")

    if status in ("sparse_new", "partial"):
        parts.append("Public benchmark metadata unavailable.")

    return " ".join(parts)
