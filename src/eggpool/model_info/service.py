"""Service skeleton for model-info subsystem."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from eggpool.model_info.dedup import canonical_needs_update
from eggpool.model_info.repository import ModelInfoRepository
from eggpool.model_info.scheduler import ModelInfoRefreshScheduler
from eggpool.model_info.sources.provider_catalog import ProviderCatalogSource
from eggpool.model_info.types import CanonicalModelInfo, ModelInfoStatus

if TYPE_CHECKING:
    from collections.abc import Iterable

    from eggpool.catalog.cache import ModelCatalogCache
    from eggpool.catalog.service import CatalogRefreshResult
    from eggpool.db.connection import Database
    from eggpool.models.config import ModelInfoConfig

logger = logging.getLogger(__name__)


class ModelInfoService:
    """Orchestrates model-info loading, reconciliation, and summary generation."""

    def __init__(
        self,
        config: ModelInfoConfig,
        db: Database,
        catalog: ModelCatalogCache,
    ) -> None:
        self._config = config
        self._db = db
        self._catalog = catalog
        self._repo = ModelInfoRepository(db)
        self._provider_source = ProviderCatalogSource(catalog)
        self._scheduler = ModelInfoRefreshScheduler(config)

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
        """Refresh provider-native observations for models due for refresh.

        Queries the repository for due rows, refreshes provider observations,
        reconciles canonical summaries, and updates next_refresh_at.
        Batch-writes all changes in a single transaction and skips rows
        where the computed payload is byte-identical to the existing row.
        """
        now = datetime.now(UTC)
        due_rows = await self._repo.list_due(
            limit=self._config.max_models_per_cycle, now=now
        )

        if not due_rows:
            return {"refreshed": 0, "total": 0, "skipped": 0}

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

            info = CanonicalModelInfo(
                model_id=model_id,
                status=status,
                summary=_generate_summary(
                    model_id=model_id,
                    status=status,
                    sparse=sparse,
                    detail=detail,
                ),
                sparse=sparse,
                detail=detail,
                provenance={
                    **existing.provenance,
                    "sources": ["provider_catalog"],
                    "reconciled_at": now.isoformat(),
                },
                conflicts=existing.conflicts,
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
