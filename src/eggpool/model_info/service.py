"""Service skeleton for model-info subsystem."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

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
    from eggpool.model_info.sources.artificial_analysis import (
        ArtificialAnalysisSource,
    )
    from eggpool.model_info.sources.base import ModelInfoSource
    from eggpool.model_info.sources.huggingface import HuggingFaceSource
    from eggpool.model_info.sources.openrouter import (
        ModelInfoHttpClient,
        OpenRouterModelInfoSource,
    )
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

        self._artificial_analysis_source: ArtificialAnalysisSource | None = None
        if config.sources.artificial_analysis.enabled and outbound_client is not None:
            from eggpool.model_info.sources.artificial_analysis import (
                ArtificialAnalysisSource,
            )

            self._artificial_analysis_source = ArtificialAnalysisSource(
                config=config.sources.artificial_analysis,
                client=outbound_client,
            )

        self._huggingface_source: HuggingFaceSource | None = None
        if config.sources.huggingface.enabled and outbound_client is not None:
            from eggpool.model_info.sources.huggingface import HuggingFaceSource

            self._huggingface_source = HuggingFaceSource(
                config=config.sources.huggingface,
                client=outbound_client,
            )

        # External source registry for iteration
        self._external_sources: dict[str, ModelInfoSource] = {}
        if self._openrouter_source is not None:
            self._external_sources["openrouter"] = self._openrouter_source
        if self._artificial_analysis_source is not None:
            self._external_sources["artificial_analysis"] = (
                self._artificial_analysis_source
            )
        if self._huggingface_source is not None:
            self._external_sources["huggingface"] = self._huggingface_source

    @property
    def repo(self) -> ModelInfoRepository:
        return self._repo

    async def load_cache(self) -> None:
        """Load provider-native observations into the DB from the catalog cache.

        Seed configured aliases (Phase A) before any external source
        fetch so source adapters that rely on exact alias matching
        (Hugging Face in particular) can resolve the very first cycle.
        """
        if not self._config.enabled:
            return
        try:
            await self.seed_configured_aliases()
        except Exception:
            logger.exception("Configured alias seeding failed")
        await self.refresh_provider_catalog_observations()

    async def seed_configured_aliases(self) -> dict[str, int]:
        """Seed ``config.model_info.aliases`` into ``model_info_aliases``.

        Returns a summary dict with ``seeded`` and ``skipped`` counts.
        Configured aliases are idempotent — re-running this method
        just refreshes ``last_seen_at`` and updates confidence. They
        are preferred over auto-discovered aliases (which are
        inserted by ``refresh_provider_catalog_observations``) and are
        what enables Hugging Face exact-alias fetches to actually
        run.

        Invalid entries (empty ``source_model_id``, unknown source)
        are skipped with a warning rather than raised; one bad
        configured alias should never block startup.
        """
        seeded = 0
        skipped = 0
        seen: set[tuple[str, str, str]] = set()
        for alias in self._config.aliases:
            source = alias.source.strip() if alias.source else ""
            source_model_id = (
                alias.source_model_id.strip() if alias.source_model_id else ""
            )
            if not source_model_id:
                logger.warning(
                    "Skipping configured alias for %s/%s: empty source_model_id",
                    alias.provider_id,
                    alias.model_id,
                )
                skipped += 1
                continue
            if not source:
                logger.warning(
                    "Skipping configured alias for %s/%s: empty source",
                    alias.provider_id,
                    alias.model_id,
                )
                skipped += 1
                continue
            if not _is_known_source(source):
                logger.warning(
                    "Configured alias %s/%s -> %s uses unknown source %r; "
                    "expected one of %s",
                    alias.provider_id,
                    alias.model_id,
                    source_model_id,
                    source,
                    sorted(_ALIAS_VALID_SOURCES),
                )
                skipped += 1
                continue

            key = (alias.provider_id, source_model_id, source)
            if key in seen:
                logger.debug(
                    "Duplicate configured alias skipped: %s/%s -> %s (%s)",
                    alias.provider_id,
                    alias.model_id,
                    source_model_id,
                    source,
                )
                skipped += 1
                continue
            seen.add(key)

            confidence = _alias_confidence_to_float(alias.confidence)
            await self._repo.upsert_alias(
                model_id=alias.model_id,
                provider_id=alias.provider_id,
                alias=source_model_id,
                source=source,
                confidence=confidence,
            )
            seeded += 1
        return {"seeded": seeded, "skipped": skipped}

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

        For every model in the catalog, ensure a canonical row exists
        with the correct status and a deterministic summary.

        The merged detail is observation-driven (Phase B): for every
        model, the latest persisted per-source observations are read
        and merged into the provider-native detail via
        :func:`build_canonical_detail`. This makes startup
        reconciliation non-destructive — previously persisted
        Hugging Face / OpenRouter / Artificial Analysis data is
        preserved across restarts even when no external source
        is currently active or matched.
        """
        model_ids = set(self._catalog._models.keys())  # pyright: ignore[reportPrivateUsage]

        created = 0
        updated = 0

        for model_id in model_ids:
            existing = await self._repo.get_canonical(model_id)
            now = datetime.now(UTC)

            status, sparse = self._classify_model(model_id)

            next_refresh = self._compute_next_refresh(status, now)

            provider_detail = self._build_detail(model_id)
            observation_payloads = await self._repo.get_latest_observation_payloads(
                model_id
            )
            merged_detail, merged_provenance, merged_conflicts = build_canonical_detail(
                model_id=model_id,
                provider_detail=provider_detail,
                observation_payloads=observation_payloads,
                existing_detail=existing.detail if existing is not None else None,
            )
            conflicts = dict(merged_conflicts)
            if existing is not None:
                # Preserve pre-existing conflict rows build_canonical_detail
                # does not manage (e.g. benchmark conflicts).
                for key, val in existing.conflicts.items():
                    conflicts.setdefault(key, val)

            if existing is None:
                first_seen = now
                last_refreshed = now
            else:
                first_seen = existing.first_seen_at
                # Reconcile is not a refresh: keep last_refreshed_at stable
                # unless the row never recorded one. This is what
                # prevents the "wiped on restart" effect.
                last_refreshed = existing.last_refreshed_at or now

            summary = _generate_summary(
                model_id=model_id,
                status=status,
                sparse=sparse,
                detail=merged_detail,
                has_benchmarks=bool(merged_detail.get("benchmarks")),
                has_hf_metadata=bool(merged_detail.get("huggingface_metadata")),
                has_conflicts=bool(conflicts),
                has_source_unavailable=False,
            )

            if existing is None:
                provenance: dict[str, object] = {
                    **merged_provenance,
                    "reconciled_at": now.isoformat(),
                    "reason": reason,
                }
                info = CanonicalModelInfo(
                    model_id=model_id,
                    status=status,
                    summary=summary,
                    sparse=sparse,
                    detail=merged_detail,
                    provenance=provenance,
                    conflicts=conflicts,
                    first_seen_at=first_seen,
                    last_seen_at=now,
                    last_refreshed_at=last_refreshed,
                    next_refresh_at=next_refresh,
                )
                await self._repo.upsert_canonical(info)
                created += 1
            else:
                provenance = {
                    **existing.provenance,
                    "sources": merged_provenance["sources"],
                    "reconciled_at": now.isoformat(),
                    "reason": reason,
                }
                info = CanonicalModelInfo(
                    model_id=model_id,
                    status=status,
                    summary=summary,
                    sparse=sparse,
                    detail=merged_detail,
                    provenance=provenance,
                    conflicts=conflicts,
                    first_seen_at=first_seen,
                    last_seen_at=now,
                    last_refreshed_at=last_refreshed,
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
                provider_detail = self._build_detail(model_id)
                observation_payloads = await self._repo.get_latest_observation_payloads(
                    model_id
                )
                merged_detail, merged_provenance, merged_conflicts = (
                    build_canonical_detail(
                        model_id=model_id,
                        provider_detail=provider_detail,
                        observation_payloads=observation_payloads,
                    )
                )
                provenance: dict[str, object] = {
                    **merged_provenance,
                    "reconciled_at": now.isoformat(),
                }
                summary = _generate_summary(
                    model_id=model_id,
                    status=status,
                    sparse=sparse,
                    detail=merged_detail,
                    has_benchmarks=bool(merged_detail.get("benchmarks")),
                    has_hf_metadata=bool(merged_detail.get("huggingface_metadata")),
                    has_conflicts=bool(merged_conflicts),
                )
                info = CanonicalModelInfo(
                    model_id=model_id,
                    status=status,
                    summary=summary,
                    sparse=sparse,
                    detail=merged_detail,
                    provenance=provenance,
                    conflicts=merged_conflicts,
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
                provider_detail = self._build_detail(model_id)
                observation_payloads = await self._repo.get_latest_observation_payloads(
                    model_id
                )
                merged_detail, merged_provenance, merged_conflicts = (
                    build_canonical_detail(
                        model_id=model_id,
                        provider_detail=provider_detail,
                        observation_payloads=observation_payloads,
                        existing_detail=existing.detail,
                    )
                )
                # Preserve any pre-existing conflict rows.
                conflicts = dict(merged_conflicts)
                for key, val in existing.conflicts.items():
                    conflicts.setdefault(key, val)
                provenance = {
                    **existing.provenance,
                    "sources": merged_provenance["sources"],
                    "reconciled_at": now.isoformat(),
                }
                info = CanonicalModelInfo(
                    model_id=model_id,
                    status=existing.status,
                    summary=existing.summary,
                    sparse=existing.sparse,
                    detail=merged_detail,
                    provenance=provenance,
                    conflicts=conflicts,
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
        attempts OpenRouter enrichment via identity resolution, attempts
        Artificial Analysis and HuggingFace enrichment for configured
        aliases, reconciles canonical summaries, and updates next_refresh_at.
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

        # Bulk-fetch Artificial Analysis catalog once per cycle
        aa_indexed: dict[str, SourceModelRecord] = {}
        if self._artificial_analysis_source is not None:
            try:
                aa_records = await self._artificial_analysis_source.fetch_all()
                aa_indexed = {r.source_model_id: r for r in aa_records}
                await self.record_source_success(
                    "artificial_analysis",
                    payload_count=len(aa_records),
                )
            except ModelInfoSourceFetchError as exc:
                logger.warning("Artificial Analysis source fetch failed: %s", exc)
                await self.record_source_error("artificial_analysis", exc)
            except Exception as exc:
                logger.exception("Artificial Analysis source unexpected error")
                await self.record_source_error("artificial_analysis", exc)

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

            # Try Artificial Analysis identity resolution
            aa_record = await _resolve_aa_record(model_id, self._repo, aa_indexed)
            if aa_record is not None:
                await self._persist_source_observation(aa_record, model_id=model_id)
                await self.record_source_success("artificial_analysis")

            # Try HuggingFace identity resolution
            hf_record: SourceModelRecord | None = None
            if self._huggingface_source is not None:
                hf_aliases = await self._repo.get_aliases_for_model(
                    model_id, source="huggingface"
                )
                if hf_aliases:
                    for alias in hf_aliases:
                        hf_record = await self._huggingface_source.fetch_one(alias)
                        if hf_record is not None:
                            await self._persist_source_observation(
                                hf_record, model_id=model_id
                            )
                            await self.record_source_success("huggingface")
                            break
                elif or_record is not None:
                    # Try using OpenRouter source_model_id as HF alias hint
                    hf_aliases_or = await self._repo.get_aliases_for_model(
                        model_id, source="openrouter"
                    )
                    for alias in hf_aliases_or:
                        if "/" in alias:
                            hf_record = await self._huggingface_source.fetch_one(alias)
                            if hf_record is not None:
                                await self._persist_source_observation(
                                    hf_record, model_id=model_id
                                )
                                await self.record_source_success("huggingface")
                                break

            # Build observation payloads (in-memory) for the canonical
            # detail builder. Only sources that actually matched this
            # cycle contribute a payload — empty/sparse sources never
            # claim provenance credit.
            observation_payloads: list[dict[str, object]] = []
            if or_record is not None:
                observation_payloads.append(
                    {
                        "source": "openrouter",
                        "source_model_id": or_record.source_model_id,
                        "observed_at": or_record.observed_at,
                        "confidence": or_record.confidence,
                        "normalized": {
                            "context_window": or_record.context_window,
                            "max_output_tokens": or_record.max_output_tokens,
                            "modalities": list(or_record.modalities),
                            "display_name": or_record.display_name,
                            "input_price_per_1k": or_record.input_price_per_1k,
                            "output_price_per_1k": or_record.output_price_per_1k,
                            **{k: v for k, v in or_record.normalized.items()},
                        },
                    }
                )
            if aa_record is not None:
                observation_payloads.append(
                    {
                        "source": "artificial_analysis",
                        "source_model_id": aa_record.source_model_id,
                        "observed_at": aa_record.observed_at,
                        "confidence": aa_record.confidence,
                        "normalized": {
                            "display_name": aa_record.display_name,
                            "benchmarks": [
                                {
                                    "name": b.benchmark_name,
                                    "score": b.score,
                                    "rank": b.rank,
                                    "percentile": b.percentile,
                                    "source": b.source,
                                    "observed_at": (
                                        b.observed_at.isoformat()
                                        if b.observed_at
                                        else None
                                    ),
                                    "notes": b.notes,
                                }
                                for b in aa_record.benchmarks
                            ],
                        },
                    }
                )
            if hf_record is not None:
                hf_normalized = dict(hf_record.normalized)
                if hf_record.license and "license" not in hf_normalized:
                    hf_normalized["license"] = hf_record.license
                observation_payloads.append(
                    {
                        "source": "huggingface",
                        "source_model_id": hf_record.source_model_id,
                        "observed_at": hf_record.observed_at,
                        "confidence": hf_record.confidence,
                        "normalized": hf_normalized,
                    }
                )

            merged_detail, merged_provenance, merged_conflicts = build_canonical_detail(
                model_id=model_id,
                provider_detail=detail,
                observation_payloads=observation_payloads,
                existing_detail=existing.detail,
            )
            conflicts = merged_conflicts
            # Preserve any pre-existing conflict rows (e.g. benchmark
            # conflicts) that build_canonical_detail doesn't manage.
            for key, val in existing.conflicts.items():
                conflicts.setdefault(key, val)

            # Override status to 'conflicting' when a material context conflict exists
            if conflicts and "context_window" in conflicts:
                status = cast("ModelInfoStatus", "conflicting")

            # Provenance: keep prior reconciled_at and other meta, but
            # let build_canonical_detail decide which sources actually
            # contributed. Existing provenance keys (e.g. lazy_created)
            # are preserved.
            provenance: dict[str, object] = {
                **existing.provenance,
                "sources": merged_provenance["sources"],
                "reconciled_at": now.isoformat(),
            }

            has_benchmarks = any(
                p.get("source") == "artificial_analysis" for p in observation_payloads
            ) and bool(aa_record and aa_record.benchmarks)
            has_hf_metadata = any(
                p.get("source") == "huggingface" for p in observation_payloads
            )

            info = CanonicalModelInfo(
                model_id=model_id,
                status=status,
                summary=_generate_summary(
                    model_id=model_id,
                    status=status,
                    sparse=sparse,
                    detail=merged_detail,
                    has_benchmarks=has_benchmarks,
                    has_hf_metadata=has_hf_metadata,
                    has_conflicts=bool(conflicts),
                    has_source_unavailable=False,
                ),
                sparse=sparse,
                detail=merged_detail,
                provenance=provenance,
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

    async def refresh_model_info(
        self,
        model_id: str,
        *,
        provider_id: str | None = None,
        source: str | None = None,
        force: bool = False,
    ) -> dict[str, object]:
        """Force-refresh a single model immediately.

        Unlike :meth:`refresh_due_models`, this method:

        * Honors a ``model_id`` argument — only that model is touched.
        * Honors ``force=True`` to bypass any ``next_refresh_at``
          gating; the row is always updated.
        * Honors ``source`` to restrict the refresh to a specific
          source (``openrouter`` / ``artificial_analysis`` /
          ``huggingface`` / ``provider_catalog``). The provider
          catalog is always refreshed so callability facts stay
          current; only the external fetches are filtered.
        * Returns a counts dict with ``requested``,
          ``refreshed``, ``skipped``, ``errors``,
          ``sources_attempted``, ``sources_matched``, and
          ``observations`` for the API endpoint.
        """
        sources_attempted: list[str] = []
        sources_matched: list[str] = []
        observations_persisted = 0

        if not model_id or not model_id.strip():
            return {
                "requested": 0,
                "refreshed": 0,
                "skipped": 0,
                "errors": 0,
                "sources_attempted": [],
                "sources_matched": [],
                "observations": 0,
            }

        model_id = model_id.strip()

        # 1. Ensure a canonical row exists. The caller is responsible
        # for passing the canonical (base) model_id; we do not
        # silently strip provider suffixes here because that would
        # silently change which row we update.
        lookup_id = model_id

        existing = await self._repo.get_canonical(lookup_id)
        if existing is None:
            await self.ensure_canonical(lookup_id)
            existing = await self._repo.get_canonical(lookup_id)
        if existing is None:
            # Defensive: ensure_canonical should never return None here.
            return {
                "requested": 1,
                "refreshed": 0,
                "skipped": 0,
                "errors": 1,
                "sources_attempted": [],
                "sources_matched": [],
                "observations": 0,
            }

        # When not forced, honor the existing next_refresh_at: a row
        # that's not yet due returns a no-op counts dict.
        now = datetime.now(UTC)
        if (
            not force
            and existing.next_refresh_at is not None
            and existing.next_refresh_at > now
        ):
            return {
                "requested": 1,
                "refreshed": 0,
                "skipped": 1,
                "errors": 0,
                "sources_attempted": [],
                "sources_matched": [],
                "observations": 0,
            }

        # 2. Always refresh the provider-catalog observation for
        #    callability facts.
        try:
            records = await self._provider_source.fetch_all()
            provider_record = None
            for record in records:
                rec_model_id = record.model_id or record.source_model_id
                if rec_model_id != lookup_id:
                    continue
                if provider_id is not None and record.provider_id != provider_id:
                    continue
                provider_record = record
                break
            if provider_record is not None:
                await self._persist_source_observation(
                    provider_record, model_id=lookup_id
                )
                observations_persisted += 1
                sources_matched.append("provider_catalog")
        except Exception as exc:
            logger.exception(
                "Provider-catalog refresh failed for %s: %s", lookup_id, exc
            )
        sources_attempted.append("provider_catalog")

        # 3. Fetch external sources (filtered by ``source`` arg).
        async def _fetch_openrouter() -> SourceModelRecord | None:
            if self._openrouter_source is None:
                return None
            try:
                records = await self._openrouter_source.fetch_all()
                or_indexed = {r.source_model_id: r for r in records}
                return await resolve_openrouter_record(
                    lookup_id, self._repo, or_indexed
                )
            except ModelInfoSourceFetchError as exc:
                logger.warning("OpenRouter fetch failed for %s: %s", lookup_id, exc)
                await self.record_source_error("openrouter", exc)
                return None
            except Exception as exc:
                logger.exception(
                    "OpenRouter unexpected error for %s: %s", lookup_id, exc
                )
                await self.record_source_error("openrouter", exc)
                return None

        async def _fetch_aa() -> SourceModelRecord | None:
            if self._artificial_analysis_source is None:
                return None
            try:
                aa_records = await self._artificial_analysis_source.fetch_all()
                await self.record_source_success(
                    "artificial_analysis",
                    payload_count=len(aa_records),
                )
            except ModelInfoSourceFetchError as exc:
                logger.warning(
                    "Artificial Analysis fetch failed for %s: %s",
                    lookup_id,
                    exc,
                )
                await self.record_source_error("artificial_analysis", exc)
                return None
            except Exception as exc:
                logger.exception(
                    "Artificial Analysis unexpected error for %s: %s",
                    lookup_id,
                    exc,
                )
                await self.record_source_error("artificial_analysis", exc)
                return None
            aa_indexed = {r.source_model_id: r for r in aa_records}
            return await _resolve_aa_record(lookup_id, self._repo, aa_indexed)

        async def _fetch_hf() -> SourceModelRecord | None:
            if self._huggingface_source is None:
                return None
            hf_aliases = await self._repo.get_aliases_for_model(
                lookup_id, source="huggingface"
            )
            for alias in hf_aliases:
                try:
                    record = await self._huggingface_source.fetch_one(alias)
                except Exception as exc:
                    logger.warning("Hugging Face fetch failed for %s: %s", alias, exc)
                    await self.record_source_error("huggingface", exc)
                    continue
                if record is not None:
                    return record
            return None

        or_record: SourceModelRecord | None = None
        aa_record: SourceModelRecord | None = None
        hf_record: SourceModelRecord | None = None

        if source in (None, "openrouter"):
            or_record = await _fetch_openrouter()
            if or_record is not None:
                await self._persist_source_observation(or_record, model_id=lookup_id)
                await self.record_source_success("openrouter")
                observations_persisted += 1
                sources_matched.append("openrouter")
            if self._openrouter_source is not None:
                sources_attempted.append("openrouter")

        if source in (None, "artificial_analysis"):
            aa_record = await _fetch_aa()
            if aa_record is not None:
                await self._persist_source_observation(aa_record, model_id=lookup_id)
                await self.record_source_success("artificial_analysis")
                observations_persisted += 1
                sources_matched.append("artificial_analysis")
            if self._artificial_analysis_source is not None:
                sources_attempted.append("artificial_analysis")

        if source in (None, "huggingface"):
            hf_record = await _fetch_hf()
            if hf_record is not None:
                await self._persist_source_observation(hf_record, model_id=lookup_id)
                await self.record_source_success("huggingface")
                observations_persisted += 1
                sources_matched.append("huggingface")
            if self._huggingface_source is not None:
                sources_attempted.append("huggingface")

        # 4. Rebuild canonical detail from provider + latest observations.
        provider_detail = self._build_detail(lookup_id)
        observation_payloads = await self._repo.get_latest_observation_payloads(
            lookup_id
        )
        merged_detail, merged_provenance, merged_conflicts = build_canonical_detail(
            model_id=lookup_id,
            provider_detail=provider_detail,
            observation_payloads=observation_payloads,
            existing_detail=existing.detail,
        )
        conflicts: dict[str, object] = dict(merged_conflicts)
        for key, val in existing.conflicts.items():
            conflicts.setdefault(key, val)

        status, sparse = self._classify_model(lookup_id)
        if conflicts and "context_window" in conflicts:
            status = cast("ModelInfoStatus", "conflicting")

        provenance: dict[str, object] = {
            **existing.provenance,
            "sources": merged_provenance["sources"],
            "reconciled_at": now.isoformat(),
            "force_refreshed": force,
            "requested_source": source,
        }

        has_benchmarks = any(
            p.get("source") == "artificial_analysis" for p in observation_payloads
        ) and bool(aa_record and aa_record.benchmarks)
        has_hf_metadata = any(
            p.get("source") == "huggingface" for p in observation_payloads
        )

        next_refresh = self._scheduler.next_refresh_for(
            status=status,
            first_seen_at=existing.first_seen_at,
            last_refreshed_at=now,
            now=now,
        )

        info = CanonicalModelInfo(
            model_id=lookup_id,
            status=status,
            summary=_generate_summary(
                model_id=lookup_id,
                status=status,
                sparse=sparse,
                detail=merged_detail,
                has_benchmarks=has_benchmarks,
                has_hf_metadata=has_hf_metadata,
                has_conflicts=bool(conflicts),
                has_source_unavailable=False,
            ),
            sparse=sparse,
            detail=merged_detail,
            provenance=provenance,
            conflicts=conflicts,
            first_seen_at=existing.first_seen_at,
            last_seen_at=now,
            last_refreshed_at=now,
            next_refresh_at=next_refresh,
        )
        if canonical_needs_update(existing, info):
            await self._repo.upsert_canonical(info)
            refreshed = 1
            skipped = 0
        else:
            refreshed = 0
            skipped = 1

        return {
            "requested": 1,
            "refreshed": refreshed,
            "skipped": skipped,
            "errors": 0,
            "sources_attempted": sources_attempted,
            "sources_matched": sources_matched,
            "observations": observations_persisted,
        }

    async def force_refresh_batch(
        self,
        *,
        batch_size: int | None = None,
    ) -> dict[str, object]:
        """Force-refresh a bounded batch of catalog models.

        Unlike :meth:`refresh_due_models` this method bypasses the
        ``next_refresh_at`` gate: every canonical row is force-refreshed
        up to ``batch_size`` rows in this single call. The batch is
        bounded so a single endpoint hit cannot block the event loop
        for minutes on large fleets; callers can invoke this method
        repeatedly to drain the catalog.

        Returns aggregate counts:

        * ``requested`` — number of canonical rows selected for refresh.
        * ``refreshed`` — rows whose canonical detail actually changed.
        * ``skipped`` — rows where the merged payload matched the
          persisted row byte-for-byte.
        * ``errors`` — exceptions raised while refreshing a row (the
          batch keeps going on per-row failure).
        * ``sources_attempted`` / ``sources_matched`` — union across
          all rows in the batch.
        * ``observations`` — total observations persisted across all
          rows.
        """
        if batch_size is None:
            batch_size = self._config.max_models_per_cycle
        # Cap the batch to a hard ceiling so a runaway caller can't
        # request tens of thousands of model refreshes in one shot.
        if batch_size <= 0:
            batch_size = 1

        all_rows = await self._repo.list_all_canonical(limit=batch_size)
        requested = len(all_rows)

        refreshed = 0
        skipped = 0
        errors = 0
        sources_attempted: set[str] = set()
        sources_matched: set[str] = set()
        observations = 0

        for canonical in all_rows:
            mid = canonical.model_id
            try:
                result = await self.refresh_model_info(mid, force=True)
            except Exception:
                logger.exception("force_refresh_batch: unhandled error for %s", mid)
                errors += 1
                continue
            refreshed += _safe_int_count(result.get("refreshed"))
            skipped += _safe_int_count(result.get("skipped"))
            observations += _safe_int_count(result.get("observations"))
            for s in cast("list[str]", result.get("sources_attempted", [])):
                sources_attempted.add(s)
            for s in cast("list[str]", result.get("sources_matched", [])):
                sources_matched.add(s)

        return {
            "requested": requested,
            "refreshed": refreshed,
            "skipped": skipped,
            "errors": errors,
            "sources_attempted": sorted(sources_attempted),
            "sources_matched": sorted(sources_matched),
            "observations": observations,
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

    async def run_backfill_missing_canonical(self) -> None:
        """Background loop that fills canonical rows for orphaned models."""
        while True:
            await asyncio.sleep(60)
            try:
                result = await self.backfill_missing_canonical()
                if result["backfilled"] > 0:
                    logger.info(
                        "Model info backfill: created %d canonical row(s)",
                        result["backfilled"],
                    )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Model info backfill failed")

    async def record_source_success(
        self,
        source_name: str,
        *,
        status_code: int | None = None,
        duration_ms: int | None = None,
        payload_count: int | None = None,
    ) -> None:
        """Record a successful fetch from a model-info source."""
        await self._repo.record_source_success(
            source_name,
            status_code=status_code,
            duration_ms=duration_ms,
            payload_count=payload_count,
        )

    async def record_source_error(
        self,
        source_name: str,
        exc: Exception,
        *,
        status_code: int | None = None,
    ) -> None:
        """Record an error from a model-info source with exponential backoff."""
        failure_count = await self._repo.get_source_failure_count(source_name)
        cooldown = _compute_source_backoff(source_name, failure_count)
        rate_limited_until = None
        if status_code == 429:
            rate_limited_until = cooldown
        await self._repo.record_source_error(
            source_name,
            exc,
            cooldown_until=cooldown,
            status_code=status_code,
            rate_limited_until=rate_limited_until,
        )

    async def get_override(self, model_id: str) -> dict[str, object] | None:
        """Return the manual override for a model, or None."""
        return await self._repo.get_override(model_id)

    async def apply_override(
        self,
        model_id: str,
        *,
        summary: str | None = None,
        family: str | None = None,
        display_name: str | None = None,
        notes: str | None = None,
        hide_benchmark_sources: bool = False,
        status_override: str | None = None,
    ) -> None:
        """Apply a manual field-level override for a model."""
        await self._repo.upsert_override(
            model_id,
            summary=summary,
            family=family,
            display_name=display_name,
            notes=notes,
            hide_benchmark_sources=hide_benchmark_sources,
            status_override=status_override,
        )

    async def remove_override(self, model_id: str) -> bool:
        """Remove a manual override. Returns True if removed."""
        return await self._repo.delete_override(model_id)

    async def get_summary(self, model_id: str) -> CanonicalModelInfo | None:
        """Return the canonical summary for a model."""
        return await self._repo.get_canonical(model_id)

    async def ensure_canonical(self, model_id: str) -> CanonicalModelInfo:
        """Return the canonical row, creating a sparse one if missing.

        The dashboard's per-model detail page is the only operator path
        that links directly to a model_id, so traffic-observed models
        that never appeared in any provider's ``/v1/models`` listing
        would otherwise show the empty-state page (``Model info not
        available``) every time. To make the page useful on every
        click we backfill a sparse canonical row on demand:

        * If the model is in the live catalog, the row uses the
          catalog's classification (``_classify_model``) and detail
          fields (``_build_detail``), with provenance ``provider_catalog``
          — identical to what ``reconcile_catalog_snapshot`` would
          have written at startup, just lazily.
        * If the model is not in the catalog (traffic-only), the row
          is marked ``status="unmatched"``, ``sparse=True``, with
          provenance ``traffic_observation`` so it cannot be confused
          with a catalog-confirmed model.

        The next periodic refresh (``run_periodic_refresh``) will
        attempt to enrich the row from external sources if any are
        enabled, and a subsequent catalog refresh will upgrade it.
        Callers must handle exceptions — a database failure here is
        not recoverable inside the service, but the dashboard's
        ``try/except`` swallows it and falls back to the empty-state
        render.
        """
        existing = await self._repo.get_canonical(model_id)
        if existing is not None:
            return existing

        in_catalog = model_id in self._catalog._models  # pyright: ignore[reportPrivateUsage]
        status, sparse = self._classify_model(model_id)
        detail = self._build_detail(model_id) if in_catalog else {}

        now = datetime.now(UTC)
        provenance: dict[str, object] = {
            "sources": ["provider_catalog" if in_catalog else "traffic_observation"],
            "reconciled_at": now.isoformat(),
            "lazy_created": True,
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
            next_refresh_at=self._compute_next_refresh(status, now),
        )
        # ``model_info_canonical.model_id`` has a FK to ``models``. A
        # traffic-only model may never have been catalogued, so seed a
        # placeholder ``models`` row inside the same transaction as the
        # canonical upsert; an existing row is left untouched via
        # ``INSERT OR IGNORE``.
        await self._repo.upsert_canonical_with_model(info)
        return info

    async def backfill_missing_canonical(self, limit: int = 50) -> dict[str, int]:
        """Create sparse canonical rows for models that lack one.

        Finds ``models`` rows without a matching ``model_info_canonical``
        row and calls ``ensure_canonical`` for each.  The background
        refresh will later enrich them from external sources.

        Runs once at startup (after ``reconcile_catalog_snapshot``) and
        periodically as a supervised background task to cover models
        that were withdrawn and later reappeared.

        Returns ``{"backfilled": N}`` where N is the number of rows
        created.  Failures for individual models are logged and skipped
        so one bad row never blocks the rest of the batch.
        """
        model_ids = await self._repo.list_models_without_canonical(limit=limit)
        backfilled = 0
        for model_id in model_ids:
            try:
                await self.ensure_canonical(model_id)
                backfilled += 1
            except Exception:
                logger.exception("Failed to backfill canonical row for %s", model_id)
        return {"backfilled": backfilled}

    async def backfill_legacy_detail_blocks(self, limit: int = 200) -> dict[str, int]:
        """Re-populate the normalized ``limits`` block on pre-Phase-B
        canonical rows.

        Phase B introduced the nested ``detail["limits"]`` block
        (``effective_context``, ``external_context``, ``effective_output``,
        ``external_output``) and made the canonical detail
        observation-driven and non-destructive.  Canonical rows written
        before Phase B only carry the legacy flat keys
        (``context_tokens`` / ``context_window_external`` /
        ``max_output_tokens``).

        This backfill finds rows whose ``detail`` lacks a populated
        ``limits`` block, rebuilds the canonical detail via
        :func:`build_canonical_detail` using the existing flat keys as
        the provider detail and any persisted observation payloads as
        the external evidence, and writes the result back.  The
        operation is idempotent: rows that already have a populated
        ``limits`` block are skipped.

        Returns ``{"scanned": N, "upgraded": M, "skipped": K, "errors": E}``
        where the counts describe the batch as a whole.  Individual
        failures are logged and counted but never block the rest.
        """
        try:
            rows: list[CanonicalModelInfo] = await self._repo.list_all_canonical(
                limit=limit
            )
        except AttributeError:
            rows = []
        upgraded = 0
        skipped = 0
        errors = 0
        for canonical in rows:
            detail = canonical.detail or {}
            raw_limits = cast("dict[str, object]", detail.get("limits", {}))
            if raw_limits and (
                raw_limits.get("effective_context") is not None
                or raw_limits.get("external_context") is not None
            ):
                skipped += 1
                continue
            try:
                provider_detail = self._build_detail(canonical.model_id)
                observation_payloads = await self._repo.get_latest_observation_payloads(
                    canonical.model_id
                )
                # Pre-seed the provider detail with the legacy flat
                # keys so ``build_canonical_detail`` can lift them
                # into the new nested ``limits`` block. Without this,
                # legacy rows that pre-date Phase B would lose their
                # context_window_external / max_output_tokens values
                # because the merge only consults ``existing_detail``
                # via the ``external_*`` prefix.
                legacy_limits = _legacy_flat_keys_to_limits(detail)
                if legacy_limits:
                    merged_provider = dict(provider_detail)
                    existing_provider_limits = cast(
                        "dict[str, object]",
                        merged_provider.get("limits", {}),
                    )
                    for key, val in legacy_limits.items():
                        if key not in existing_provider_limits:
                            existing_provider_limits[key] = val
                    merged_provider["limits"] = existing_provider_limits
                    provider_detail = merged_provider
                merged_detail, merged_provenance, merged_conflicts = (
                    build_canonical_detail(
                        model_id=canonical.model_id,
                        provider_detail=provider_detail,
                        observation_payloads=observation_payloads,
                        existing_detail=detail,
                    )
                )
                conflicts: dict[str, object] = dict(merged_conflicts)
                for key, val in canonical.conflicts.items():
                    conflicts.setdefault(key, val)
                provenance = dict(canonical.provenance)
                provenance["sources"] = merged_provenance["sources"]
                provenance["backfilled_limits"] = True
                updated = CanonicalModelInfo(
                    model_id=canonical.model_id,
                    status=canonical.status,
                    summary=canonical.summary,
                    sparse=canonical.sparse,
                    detail=merged_detail,
                    provenance=provenance,
                    conflicts=conflicts,
                    first_seen_at=canonical.first_seen_at,
                    last_seen_at=canonical.last_seen_at,
                    last_refreshed_at=canonical.last_refreshed_at,
                    next_refresh_at=canonical.next_refresh_at,
                )
                if canonical_needs_update(canonical, updated):
                    await self._repo.upsert_canonical(updated)
                    upgraded += 1
                else:
                    skipped += 1
            except Exception:
                logger.exception(
                    "Failed to backfill detail block for %s", canonical.model_id
                )
                errors += 1
        return {
            "scanned": len(rows),
            "upgraded": upgraded,
            "skipped": skipped,
            "errors": errors,
        }

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
            limits_block: dict[str, object] = dict(
                cast("dict[str, object]", detail.get("limits", {}))
            )
            if limits.context_tokens is not None:
                limits_block["effective_context"] = limits.context_tokens
                # Legacy flat key kept for migration; renderer prefers limits.*.
                detail["context_tokens"] = limits.context_tokens
            if limits.input_tokens is not None:
                limits_block["effective_input"] = limits.input_tokens
                detail["input_tokens"] = limits.input_tokens
            if limits.output_tokens is not None:
                limits_block["effective_output"] = limits.output_tokens
                detail["output_tokens"] = limits.output_tokens
            if limits_block:
                detail["limits"] = limits_block

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


def _enrich_detail_from_record(  # pyright: ignore[reportUnusedFunction]
    detail: dict[str, object],
    record: SourceModelRecord | None,
) -> dict[str, object]:
    """Enrich canonical detail with external-sourced fields.

    External fields are stored under explicit ``external_*`` keys so they
    never overwrite provider-native values.  The existing ``display_name``
    and ``context_tokens`` fields remain authoritative from the catalog.
    """
    if record is None:
        return dict(detail)

    enriched = dict(detail)

    source = record.source

    # External IDs
    external_ids = dict(cast("dict[str, object]", enriched.get("external_ids", {})))
    external_ids[source] = record.source_model_id
    enriched["external_ids"] = external_ids

    # External context/output (advisory only). Stored under
    # detail["limits"]["external_*"] in the normalized schema, with
    # legacy flat keys kept for back-compat.
    if record.context_window is not None:
        limits_block = dict(cast("dict[str, object]", enriched.get("limits", {})))
        limits_block["external_context"] = record.context_window
        enriched["limits"] = limits_block
        enriched["context_window_external"] = record.context_window

    if record.max_output_tokens is not None:
        limits_block = dict(cast("dict[str, object]", enriched.get("limits", {})))
        limits_block["external_output"] = record.max_output_tokens
        enriched["limits"] = limits_block
        enriched["max_output_tokens_external"] = record.max_output_tokens

    # External modalities (advisory only)
    if record.modalities:
        enriched["modalities_external"] = sorted(record.modalities)

    # Pricing observation (advisory, never cost-calculation truth)
    if record.input_price_per_1k is not None or record.output_price_per_1k is not None:
        pricing_obs = dict(
            cast("dict[str, object]", enriched.get("pricing_observation", {}))
        )
        if record.input_price_per_1k is not None:
            pricing_obs["input_price_per_1k"] = record.input_price_per_1k
        if record.output_price_per_1k is not None:
            pricing_obs["output_price_per_1k"] = record.output_price_per_1k
        enriched["pricing_observation"] = pricing_obs

    # Display name from external source (non-authoritative)
    if record.display_name and record.display_name != record.source_model_id:
        enriched[f"display_name_{source}"] = record.display_name

    # Created timestamp (if present)
    normalized = record.normalized
    created_at = normalized.get("created_at")
    if created_at is not None:
        enriched["created_at_external"] = created_at

    # Benchmarks (from Artificial Analysis or other sources)
    if record.benchmarks:
        existing_benchmarks = list(cast("list[object]", enriched.get("benchmarks", [])))
        for b in record.benchmarks:
            existing_benchmarks.append(
                {
                    "name": b.benchmark_name,
                    "score": b.score,
                    "rank": b.rank,
                    "percentile": b.percentile,
                    "source": b.source,
                    "notes": b.notes,
                    "observed_at": (
                        b.observed_at.isoformat() if b.observed_at else None
                    ),
                }
            )
        enriched["benchmarks"] = existing_benchmarks

    # Hugging Face metadata (compact, never full card text)
    if source == "huggingface":
        hf_meta: dict[str, object] = {}
        if record.license:
            hf_meta["license"] = record.license
        normalized_hf = record.normalized
        for key in (
            "pipeline_tag",
            "library_name",
            "model_type",
            "tags",
            "downloads",
            "likes",
        ):
            val = normalized_hf.get(key)
            if val is not None:
                hf_meta[key] = val
        if hf_meta:
            enriched["huggingface_metadata"] = hf_meta

    return enriched


def _detect_context_conflicts(  # pyright: ignore[reportUnusedFunction]
    detail: dict[str, object],
    record: SourceModelRecord | None,
    existing_conflicts: dict[str, object],
) -> dict[str, object]:
    """Detect and record conflicts between provider-native and external metadata.

    A conflict is recorded when both provider-local and external values exist
    for context_window and differ materially (>10% relative difference).

    Reads the provider-native effective context from the normalized
    ``detail["limits"]["effective_context"]`` block, with a
    back-compat fallback to the legacy flat ``detail["context_tokens"]``
    key. New canonical rows always populate the nested limits block,
    but rows from older installs may still carry only the flat key.
    """
    conflicts = dict(existing_conflicts)

    if record is None:
        return conflicts

    limits_block = cast("dict[str, object]", detail.get("limits", {}))
    local_ctx = limits_block.get("effective_context")
    if local_ctx is None:
        local_ctx = detail.get("context_tokens")
    ext_ctx = record.context_window

    if (
        isinstance(local_ctx, (int, float))
        and local_ctx > 0
        and ext_ctx is not None
        and ext_ctx > 0
    ):
        diff = abs(local_ctx - ext_ctx)
        relative = diff / max(local_ctx, ext_ctx)
        if relative > 0.10:
            conflicts["context_window"] = {
                "provider_catalog": local_ctx,
                record.source: ext_ctx,
                "selected": "provider_catalog/effective_limit",
                "reason": "local/provider effective limit wins for Eggpool display",
            }

    return conflicts


def _detect_benchmark_conflicts(  # pyright: ignore[reportUnusedFunction]
    aa_record: SourceModelRecord | None,
    existing_conflicts: dict[str, object],
    current_conflicts: dict[str, object],
) -> dict[str, object]:
    """Detect conflicts between benchmark sources.

    Artificial Analysis benchmark rows conflict with Hugging Face
    model-card claims only if both claim the same benchmark name/version
    with different numeric values.
    """
    conflicts = dict(current_conflicts)
    if aa_record is None or not aa_record.benchmarks:
        return conflicts

    # Check if existing detail has benchmarks from other sources
    # For now, just record AA benchmarks as non-conflicting observations
    # Real conflicts would require comparing against HF leaderboard data
    return conflicts


def _safe_int_count(value: Any) -> int:
    """Coerce a count value from a result dict to a non-negative int.

    Used to defensively read counts returned from
    :meth:`refresh_model_info` and other helpers that pass dict[str,
    object] across the public surface. ``None`` and any non-numeric
    value collapse to ``0`` so pyright stays happy and the aggregator
    never raises on unexpected payloads.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    return 0


def _normalize_observation_payload(
    source: str,
    payload: dict[str, object],
) -> dict[str, object]:
    """Convert a persisted ``normalized_json`` payload to a partial detail.

    Used by :func:`build_canonical_detail` so that an external
    observation row (which has no typed ``SourceModelRecord`` fields
    to bind to) still contributes a useful fragment to the merged
    canonical detail.
    """
    out: dict[str, object] = {}
    if source == "huggingface":
        hf_meta: dict[str, object] = {}
        for key in (
            "pipeline_tag",
            "library_name",
            "model_type",
            "tags",
            "downloads",
            "likes",
            "card_data",
            "card_metadata",
        ):
            val = payload.get(key)
            if val is not None:
                hf_meta[key] = val
        license_val = payload.get("license")
        if license_val is not None:
            hf_meta["license"] = license_val
        if hf_meta:
            out["huggingface_metadata"] = hf_meta
        modalities: list[str] = []
        for m in cast("list[object]", payload.get("modalities", []) or []):
            modalities.append(str(m))
        if modalities:
            out["modalities_external"] = sorted(modalities)
    elif source == "openrouter":
        ctx = payload.get("context_window") or payload.get("context_length")
        if isinstance(ctx, (int, float)) and ctx > 0:
            limits = cast("dict[str, object]", out.get("limits", {}))
            limits["external_context"] = int(ctx)
            out["limits"] = limits
        max_out = payload.get("max_output_tokens")
        if isinstance(max_out, (int, float)) and max_out > 0:
            limits = cast("dict[str, object]", out.get("limits", {}))
            limits["external_output"] = int(max_out)
            out["limits"] = limits
        modalities: list[str] = []
        for m in cast("list[object]", payload.get("modalities", []) or []):
            modalities.append(str(m))
        if modalities:
            out["modalities_external"] = sorted(modalities)
        if payload.get("display_name"):
            out["display_name_openrouter"] = payload["display_name"]
        # Advisory pricing from OpenRouter — surfaced as
        # ``pricing_observation`` so callers can show $/Mtok figures
        # without the values colliding with provider-reported pricing.
        pricing_obs: dict[str, object] = {}
        in_price = payload.get("input_price_per_1k")
        if isinstance(in_price, (int, float)) and in_price >= 0:
            pricing_obs["input_price_per_1k"] = float(in_price)
        out_price = payload.get("output_price_per_1k")
        if isinstance(out_price, (int, float)) and out_price >= 0:
            pricing_obs["output_price_per_1k"] = float(out_price)
        if pricing_obs:
            pricing_obs["source"] = "openrouter"
            out["pricing_observation"] = pricing_obs
    elif source == "artificial_analysis":
        benchmarks = payload.get("benchmarks")
        if isinstance(benchmarks, list) and benchmarks:
            out["benchmarks"] = list(cast("list[object]", benchmarks))
        if payload.get("display_name"):
            out["display_name_artificial_analysis"] = payload["display_name"]
    return out


def _legacy_flat_keys_to_limits(
    detail: dict[str, object],
) -> dict[str, object]:
    """Lift pre-Phase-B flat keys into the nested ``limits`` block.

    Returns a dict suitable for merging into ``provider_detail["limits"]``
    so that legacy rows contribute their previously known external
    values to the rebuilt canonical detail.  Recognized flat keys:

    * ``context_tokens`` → ``effective_context``
    * ``context_window_external`` → ``external_context``
    * ``max_output_tokens`` → ``effective_output``
    * ``max_output_tokens_external`` → ``external_output``
    """
    limits: dict[str, object] = {}
    ctx = detail.get("context_tokens")
    if isinstance(ctx, (int, float)) and ctx > 0:
        limits["effective_context"] = int(ctx)
    ext_ctx = detail.get("context_window_external")
    if isinstance(ext_ctx, (int, float)) and ext_ctx > 0:
        limits["external_context"] = int(ext_ctx)
    eff_out = detail.get("max_output_tokens")
    if isinstance(eff_out, (int, float)) and eff_out > 0:
        limits["effective_output"] = int(eff_out)
    ext_out = detail.get("max_output_tokens_external")
    if isinstance(ext_out, (int, float)) and ext_out > 0:
        limits["external_output"] = int(ext_out)
    return limits


def build_canonical_detail(
    *,
    model_id: str,
    provider_detail: dict[str, object],
    observation_payloads: list[dict[str, object]],
    existing_detail: dict[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    """Build a merged canonical detail, provenance, and conflict map.

    Returns ``(detail, provenance, conflicts)``. The merge rules:

    * ``provider_detail`` is the seed (it is the only authoritative
      source for ``effective_context``, ``effective_input``,
      ``effective_output``, ``protocol``, ``supports_tools``,
      ``supports_vision``, ``display_name``). Those fields are
      authoritative and never overwritten by external observations.
    * External observation payloads add *advisory* fields under
      ``limits.external_*``, ``modalities_external``, ``benchmarks``,
      ``huggingface_metadata``, ``display_name_<source>``, and
      ``external_ids``. They never overwrite provider-native values
      but enrich the detail where the provider does not speak.
    * ``existing_detail`` (if present) contributes fields that are
      *missing* from both the provider and the latest observations
      — this is the back-compat path so that a one-cycle external
      fetch failure does not erase previously persisted external
      enrichment.
    * ``provenance.sources`` lists only sources whose data actually
      contributed a field to the merged detail.
    * ``conflicts`` is built field-by-field; currently only context
      window is compared, with provider-native value selected.
    """
    detail: dict[str, object] = dict(provider_detail)

    # Start with provider limits block (or empty) so subsequent merges
    # write into the same shape regardless of which path populated it.
    limits_block: dict[str, object] = dict(
        cast("dict[str, object]", detail.get("limits", {}))
    )

    used_sources: set[str] = set()
    external_ids: dict[str, object] = dict(
        cast("dict[str, object]", detail.get("external_ids", {}))
    )
    benchmarks: list[object] = list(cast("list[object]", detail.get("benchmarks", [])))
    hf_meta: dict[str, object] = dict(
        cast("dict[str, object]", detail.get("huggingface_metadata", {}))
    )
    modalities_external: set[str] = set(
        cast("list[str]", detail.get("modalities_external", []))
    )
    conflicts: dict[str, object] = {}

    for obs in observation_payloads:
        source = str(obs.get("source", ""))
        if not source or source == "provider_catalog":
            continue
        payload = cast("dict[str, object]", obs.get("normalized", {}))
        if not payload:
            continue
        source_model_id = obs.get("source_model_id")
        if source_model_id and not external_ids.get(source):
            external_ids[source] = source_model_id
        fragment = _normalize_observation_payload(source, payload)

        contributed = False
        if "limits" in fragment:
            new_limits = cast("dict[str, object]", fragment["limits"])
            for key, val in new_limits.items():
                # External-* keys from observations are authoritative
                # — overwrite stale legacy seeds so the rebuild path
                # can pick up fresher OpenRouter/AA values that
                # arrived after the legacy flat key was written.
                if (
                    key.startswith("external_")
                    or key not in limits_block
                    or limits_block[key] is None
                ):
                    limits_block[key] = val
                    contributed = True
        if "modalities_external" in fragment:
            new_modalities = cast("list[str]", fragment["modalities_external"])
            before = len(modalities_external)
            modalities_external.update(new_modalities)
            if len(modalities_external) > before:
                contributed = True
        if "benchmarks" in fragment:
            new_benchmarks = cast("list[object]", fragment["benchmarks"])
            if new_benchmarks:
                benchmarks.extend(new_benchmarks)
                contributed = True
        if "huggingface_metadata" in fragment:
            new_hf = cast("dict[str, object]", fragment["huggingface_metadata"])
            for key, val in new_hf.items():
                if key not in hf_meta or hf_meta[key] is None:
                    hf_meta[key] = val
                    contributed = True
        for key, val in fragment.items():
            if key in {
                "limits",
                "modalities_external",
                "benchmarks",
                "huggingface_metadata",
                "pricing_observation",
            }:
                continue
            if key not in detail or detail[key] in (None, ""):
                detail[key] = val
                contributed = True
        if "pricing_observation" in fragment:
            pricing_obs = cast("dict[str, object]", fragment["pricing_observation"])
            existing_pricing = cast(
                "dict[str, object]", detail.get("pricing_observation", {})
            )
            for pk, pv in pricing_obs.items():
                if pk not in existing_pricing:
                    existing_pricing[pk] = pv
            detail["pricing_observation"] = existing_pricing
            contributed = True
        if contributed:
            used_sources.add(source)

    # Context-window conflict detection (advisory: provider wins)
    eff_ctx = limits_block.get("effective_context")
    ext_ctx = limits_block.get("external_context")
    # Identify the source that contributed the external context, so
    # the conflict record names it explicitly.
    ext_source = ""
    for obs in observation_payloads:
        source = str(obs.get("source", ""))
        if source and source != "provider_catalog":
            payload = cast("dict[str, object]", obs.get("normalized", {}))
            ctx = payload.get("context_window") or payload.get("context_length")
            if (
                isinstance(ctx, (int, float))
                and ext_ctx is not None
                and isinstance(ext_ctx, (int, float))
                and int(ctx) == int(ext_ctx)
            ):
                ext_source = source
                break
    if (
        isinstance(eff_ctx, (int, float))
        and eff_ctx > 0
        and isinstance(ext_ctx, (int, float))
        and ext_ctx > 0
    ):
        diff = abs(eff_ctx - ext_ctx)
        relative = diff / max(eff_ctx, ext_ctx)
        if relative > 0.10:
            conflict_entry: dict[str, object] = {
                "provider_catalog": eff_ctx,
                "selected": "provider_catalog/effective_limit",
                "reason": ("local/provider effective limit wins for Eggpool display"),
            }
            if ext_source:
                conflict_entry[ext_source] = ext_ctx
            else:
                conflict_entry["external"] = ext_ctx
            conflicts["context_window"] = conflict_entry

    # Fallback: if external data was unavailable this cycle, preserve
    # previously known external fields rather than wiping them. Only
    # adopt a value from existing_detail when neither the provider
    # nor the latest observations contributed one.
    if existing_detail:
        for key, val in existing_detail.items():
            if key in {"limits", "huggingface_metadata", "benchmarks"}:
                continue
            if (
                (key not in detail or detail[key] in (None, ""))
                and val is not None
                and val != ""
            ):
                detail[key] = val
        existing_limits = cast("dict[str, object]", existing_detail.get("limits", {}))
        for key, val in existing_limits.items():
            if key.startswith("external_") and (
                key not in limits_block or limits_block[key] is None
            ):
                limits_block[key] = val
        existing_hf = cast(
            "dict[str, object]",
            existing_detail.get("huggingface_metadata", {}),
        )
        for key, val in existing_hf.items():
            if key not in hf_meta or hf_meta[key] is None:
                hf_meta[key] = val
        existing_benchmarks = cast(
            "list[object]", existing_detail.get("benchmarks", [])
        )
        for b in existing_benchmarks:
            if b not in benchmarks:
                benchmarks.append(b)
        existing_modalities = cast(
            "list[str]", existing_detail.get("modalities_external", [])
        )
        modalities_external.update(existing_modalities)
        existing_ext_ids = cast(
            "dict[str, object]", existing_detail.get("external_ids", {})
        )
        for k, v in existing_ext_ids.items():
            if k not in external_ids:
                external_ids[k] = v
        existing_pricing_obs = cast(
            "dict[str, object]",
            existing_detail.get("pricing_observation", {}),
        )
        if existing_pricing_obs:
            detail_pricing = cast(
                "dict[str, object]", detail.get("pricing_observation", {})
            )
            for pk, pv in existing_pricing_obs.items():
                if pk not in detail_pricing:
                    detail_pricing[pk] = pv
            detail["pricing_observation"] = detail_pricing

    # Materialize the merged detail.
    if limits_block:
        detail["limits"] = limits_block
        if "effective_context" in limits_block:
            detail["context_tokens"] = limits_block["effective_context"]
        if "external_context" in limits_block:
            detail["context_window_external"] = limits_block["external_context"]
        if "external_output" in limits_block:
            detail["max_output_tokens_external"] = limits_block["external_output"]
    if external_ids:
        detail["external_ids"] = external_ids
    if benchmarks:
        detail["benchmarks"] = benchmarks
    if hf_meta:
        detail["huggingface_metadata"] = hf_meta
    if modalities_external:
        detail["modalities_external"] = sorted(modalities_external)
    # Materialize modalities union (provider + external).
    base_modalities: set[str] = set(cast("list[str]", detail.get("modalities", [])))
    if modalities_external:
        base_modalities.update(modalities_external)
    if base_modalities:
        detail["modalities"] = sorted(base_modalities)

    # Provenance: only sources that actually contributed. Provider
    # catalog is always credited when it seeded the row.
    provider_seeded = bool(provider_detail)
    sources: list[str] = []
    if provider_seeded and "provider_catalog" not in sources:
        sources.append("provider_catalog")
    for src in sorted(used_sources):
        if src not in sources:
            sources.append(src)
    provenance: dict[str, object] = {"sources": sources}

    return detail, provenance, conflicts


def _build_source_list(  # pyright: ignore[reportUnusedFunction]
    provenance: dict[str, object],
    *,
    has_openrouter: bool = False,
    has_aa: bool = False,
    has_hf: bool = False,
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
    if has_aa and "artificial_analysis" not in sources:
        sources.append("artificial_analysis")
    if has_hf and "huggingface" not in sources:
        sources.append("huggingface")
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


async def _resolve_aa_record(
    model_id: str,
    repo: ModelInfoRepository,
    aa_indexed: dict[str, SourceModelRecord],
) -> SourceModelRecord | None:
    """Resolve a local model_id to an Artificial Analysis source record.

    Uses exact alias matching only — no fuzzy matching. Resolution
    rules mirror :func:`resolve_openrouter_record`:

    1. Exact ``model_info_aliases`` row with ``source=artificial_analysis``.
    2. Direct ``source_model_id == model_id`` match.
    """
    if not aa_indexed:
        return None

    alias_strings = await repo.get_aliases_for_model(
        model_id, source="artificial_analysis"
    )
    if len(alias_strings) == 1:
        record = aa_indexed.get(alias_strings[0])
        if record is not None:
            return record
    elif len(alias_strings) > 1:
        logger.debug(
            "Ambiguous Artificial Analysis aliases for %s: %s — skipping",
            model_id,
            alias_strings,
        )
        return None

    direct = aa_indexed.get(model_id)
    if direct is not None:
        return direct

    return None


_ALIAS_VALID_SOURCES: frozenset[str] = frozenset(
    {"provider_catalog", "openrouter", "artificial_analysis", "huggingface", "pricing"}
)

_ALIAS_CONFIDENCE_BY_NAME: dict[str, float] = {
    "exact": 1.0,
    "curated": 0.9,
    "high": 0.9,
    "medium": 0.6,
    "low": 0.3,
}


def _alias_confidence_to_float(confidence: str | float | int | None) -> float:
    """Normalize a configured alias confidence into ``[0.0, 1.0]``.

    String values map to ``_ALIAS_CONFIDENCE_BY_NAME``; numeric values
    are clamped. ``None`` and unknown strings fall back to ``0.5``
    (the repository default).
    """
    if confidence is None:
        return 0.5
    if isinstance(confidence, bool):
        return 1.0 if confidence else 0.5
    if isinstance(confidence, (int, float)):
        return max(0.0, min(1.0, float(confidence)))
    key = str(confidence).strip().lower()
    if not key:
        return 0.5
    return _ALIAS_CONFIDENCE_BY_NAME.get(key, 0.5)


def _is_known_source(source: str) -> bool:
    """Return True if *source* is one of the configured model-info sources."""
    return source in _ALIAS_VALID_SOURCES


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
    has_benchmarks: bool = False,
    has_hf_metadata: bool = False,
    has_conflicts: bool = False,
    has_source_unavailable: bool = False,
) -> str:
    """Generate a deterministic summary string for a model."""
    if status == "conflicting":
        return "Metadata conflict detected. Manual review recommended."

    parts: list[str] = []

    if sparse:
        parts.append(
            "New model detected; metadata sparse. "
            "Eggpool will refresh external sources more frequently for now."
        )

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

    if has_benchmarks:
        parts.append(
            "Benchmark metadata available from Artificial Analysis; "
            "local latency and reliability may differ."
        )

    if has_hf_metadata:
        parts.append(
            "Open-weight model metadata available from Hugging Face; "
            "benchmark data not independently verified."
        )

    if has_conflicts:
        parts.append(
            "Metadata conflict detected for context window; "
            "Eggpool is using local/provider effective limits for display."
        )

    if has_source_unavailable:
        parts.append(
            "Cached metadata is available, but one or more external sources "
            "are currently unavailable."
        )

    if status in ("sparse_new", "partial") and not has_benchmarks:
        parts.append("Public benchmark metadata unavailable.")

    return " ".join(parts)
