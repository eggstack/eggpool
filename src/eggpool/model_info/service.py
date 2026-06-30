"""Service skeleton for model-info subsystem."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from eggpool.model_info.repository import ModelInfoRepository
from eggpool.model_info.sources.provider_catalog import ProviderCatalogSource
from eggpool.model_info.types import CanonicalModelInfo, ModelInfoStatus

if TYPE_CHECKING:
    from collections.abc import Iterable

    from eggpool.catalog.cache import ModelCatalogCache
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
        """
        provider_entries = {
            (mid, pid): entry
            for (mid, pid), entry in self._catalog._provider_models.items()  # pyright: ignore[reportPrivateUsage]
            if mid == model_id
        }

        if not provider_entries:
            return (cast("ModelInfoStatus", "unmatched"), True)

        has_display_name = False
        has_context_limit = False
        has_tools_or_vision = False
        has_benchmarks = False
        has_family = False

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

        indicators = [has_display_name, has_context_limit, has_tools_or_vision]
        missing = sum(1 for i in indicators if not i)

        sparse = missing >= 2

        if sparse:
            return (cast("ModelInfoStatus", "sparse_new"), True)

        if not has_benchmarks and not has_family:
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
