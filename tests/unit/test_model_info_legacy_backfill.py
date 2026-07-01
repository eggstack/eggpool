"""Tests for the Phase F legacy-detail backfill.

Phase F of the model-info corrective plan: canonical rows written
before Phase B (which introduced the nested ``detail["limits"]``
block) need to be upgraded in place so the new display schema works
for every existing row, not just rows written by current code.

The ``backfill_legacy_detail_blocks`` method on
``ModelInfoService`` must:

* Skip rows whose ``detail`` already carries a populated ``limits``
  block (idempotent).
* Re-populate ``detail["limits"]`` for legacy rows by running
  :func:`build_canonical_detail` with the existing flat keys as
  the provider detail and any persisted observations as external
  evidence.
* Persist provenance ``backfilled_limits=True`` so operators can
  tell which rows were touched by the repair.
* Return ``scanned / upgraded / skipped / errors`` counts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eggpool.catalog.cache import ModelCatalogCache
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.model_info.repository import ModelInfoRepository
from eggpool.model_info.service import ModelInfoService
from eggpool.model_info.types import CanonicalModelInfo, SourceModelRecord
from eggpool.models.config import ModelInfoConfig


async def _run_migrations(db: Database) -> None:
    runner = MigrationRunner(db)
    await runner.run()


async def _seed_model(db: Database, model_id: str) -> None:
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO models (model_id, display_name) VALUES (?, ?)",
            (model_id, model_id),
        )


def _make_cache(model_id: str, *, context: int = 128000) -> ModelCatalogCache:
    cache = ModelCatalogCache()
    now_ts = datetime.now(UTC).timestamp()
    cache._models[model_id] = {
        "model_id": model_id,
        "display_name": model_id,
        "protocol": "openai",
        "capabilities": {"supports_tools": True},
        "source_metadata": {},
        "first_seen_at": now_ts,
        "last_seen_at": now_ts,
        "discovered_limits": {},
        "effective_limits": {
            "context_tokens": context,
            "input_tokens": context,
            "output_tokens": 16384,
            "enforce": True,
        },
    }
    cache._provider_models[(model_id, "openai")] = dict(cache._models[model_id])
    return cache


def _legacy_canonical(model_id: str) -> CanonicalModelInfo:
    """Build a canonical row whose detail only has the legacy flat
    keys, no nested ``limits`` block."""
    now = datetime.now(UTC)
    return CanonicalModelInfo(
        model_id=model_id,
        status="partial",
        summary="legacy",
        sparse=False,
        detail={
            "providers": ["openai"],
            "context_tokens": 128000,
            "context_window_external": 1_000_000,
            "max_output_tokens": 16384,
        },
        provenance={
            "sources": ["provider_catalog", "openrouter"],
            "reconciled_at": (now - timedelta(days=1)).isoformat(),
        },
        conflicts={},
        first_seen_at=now - timedelta(days=2),
        last_seen_at=now - timedelta(hours=1),
        last_refreshed_at=now - timedelta(hours=1),
        next_refresh_at=now + timedelta(hours=1),
    )


class TestBackfillLegacyDetailBlocks:
    @pytest.mark.asyncio()
    async def test_legacy_row_is_upgraded_with_nested_limits(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-4o")

            repo = ModelInfoRepository(db)
            await repo.upsert_canonical(_legacy_canonical("gpt-4o"))

            cache = _make_cache("gpt-4o")
            service = ModelInfoService(ModelInfoConfig(), db, cache)
            result = await service.backfill_legacy_detail_blocks()
            assert result["scanned"] == 1
            assert result["upgraded"] == 1
            assert result["errors"] == 0

            # Reload and verify the row now has nested limits.
            updated = await repo.get_canonical("gpt-4o")
            assert updated is not None
            limits = updated.detail.get("limits")
            assert limits is not None
            assert limits.get("effective_context") == 128000
            assert limits.get("external_context") == 1_000_000
            # Legacy flat keys are also retained for back-compat.
            assert updated.detail.get("context_tokens") == 128000
            # Provenance marks the row as backfilled.
            assert updated.provenance.get("backfilled_limits") is True
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_modern_row_with_limits_block_is_skipped(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-4o")

            repo = ModelInfoRepository(db)
            now = datetime.now(UTC)
            modern = CanonicalModelInfo(
                model_id="gpt-4o",
                status="fresh",
                summary="modern",
                sparse=False,
                detail={
                    "providers": ["openai"],
                    "limits": {"effective_context": 200000},
                },
                provenance={"sources": ["provider_catalog"]},
                conflicts={},
                first_seen_at=now - timedelta(days=1),
                last_seen_at=now,
                last_refreshed_at=now,
                next_refresh_at=now + timedelta(hours=1),
            )
            await repo.upsert_canonical(modern)

            cache = _make_cache("gpt-4o")
            service = ModelInfoService(ModelInfoConfig(), db, cache)
            result = await service.backfill_legacy_detail_blocks()
            assert result["scanned"] == 1
            assert result["upgraded"] == 0
            assert result["skipped"] == 1

            # Detail is unchanged.
            updated = await repo.get_canonical("gpt-4o")
            assert updated is not None
            assert updated.detail["limits"]["effective_context"] == 200000
            # No provenance marker added (row was not touched).
            assert "backfilled_limits" not in updated.provenance
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_legacy_row_with_external_observation_includes_or_context(
        self,
    ) -> None:
        """A legacy row with a persisted OpenRouter observation row
        should pick up ``external_context`` from the observation."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "openai/gpt-4o")

            repo = ModelInfoRepository(db)
            await repo.upsert_canonical(_legacy_canonical("openai/gpt-4o"))
            or_record = SourceModelRecord(
                source="openrouter",
                source_model_id="openai/gpt-4o",
                observed_at=datetime.now(UTC),
                raw_hash="legacy-hash",
                raw_payload={},
                normalized={"context_window": 2_000_000},
                context_window=2_000_000,
            )
            await repo.upsert_observation(
                or_record, model_id="openai/gpt-4o", provider_id="openai"
            )

            cache = _make_cache("openai/gpt-4o")
            service = ModelInfoService(ModelInfoConfig(), db, cache)
            result = await service.backfill_legacy_detail_blocks()
            assert result["upgraded"] == 1

            updated = await repo.get_canonical("openai/gpt-4o")
            assert updated is not None
            limits = updated.detail.get("limits", {})
            # Effective context from provider.
            assert limits.get("effective_context") == 128000
            # External context from the OpenRouter observation.
            assert limits.get("external_context") == 2_000_000
            assert "openrouter" in updated.provenance["sources"]
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_empty_detail_block_is_backfilled(self) -> None:
        """Rows whose detail is an empty dict should still get a
        populated limits block when the provider cache has data."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-4o")

            repo = ModelInfoRepository(db)
            now = datetime.now(UTC)
            empty_detail = CanonicalModelInfo(
                model_id="gpt-4o",
                status="sparse_new",
                summary="empty",
                sparse=True,
                detail={},
                provenance={"sources": ["provider_catalog"]},
                conflicts={},
                first_seen_at=now,
                last_seen_at=now,
                last_refreshed_at=None,
                next_refresh_at=now + timedelta(hours=1),
            )
            await repo.upsert_canonical(empty_detail)

            cache = _make_cache("gpt-4o")
            service = ModelInfoService(ModelInfoConfig(), db, cache)
            result = await service.backfill_legacy_detail_blocks()
            assert result["upgraded"] == 1

            updated = await repo.get_canonical("gpt-4o")
            assert updated is not None
            assert "limits" in updated.detail
            assert updated.detail["limits"]["effective_context"] == 128000
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_repair_merges_latest_hf_observation_into_canonical_detail(
        self,
    ) -> None:
        """A legacy row with a persisted Hugging Face observation row
        should pick up ``huggingface_metadata`` from the observation
        during the repair cycle."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "meta-llama/Llama-3.1-8B-Instruct")

            repo = ModelInfoRepository(db)
            await repo.upsert_canonical(
                _legacy_canonical("meta-llama/Llama-3.1-8B-Instruct")
            )
            hf_record = SourceModelRecord(
                source="huggingface",
                source_model_id="meta-llama/Llama-3.1-8B-Instruct",
                observed_at=datetime.now(UTC),
                raw_hash="hf-legacy-hash",
                raw_payload={},
                normalized={
                    "pipeline_tag": "text-generation",
                    "library_name": "transformers",
                    "downloads": 12345,
                    "likes": 678,
                    "tags": ["text-generation", "llama"],
                    "license": "llama3.1",
                },
            )
            await repo.upsert_observation(
                hf_record,
                model_id="meta-llama/Llama-3.1-8B-Instruct",
                provider_id="openai",
            )

            cache = _make_cache("meta-llama/Llama-3.1-8B-Instruct")
            service = ModelInfoService(ModelInfoConfig(), db, cache)
            result = await service.backfill_legacy_detail_blocks()
            assert result["upgraded"] == 1

            updated = await repo.get_canonical("meta-llama/Llama-3.1-8B-Instruct")
            assert updated is not None
            hf_meta = updated.detail.get("huggingface_metadata", {})
            assert hf_meta.get("pipeline_tag") == "text-generation"
            assert hf_meta.get("library_name") == "transformers"
            assert hf_meta.get("downloads") == 12345
            assert hf_meta.get("likes") == 678
            assert hf_meta.get("license") == "llama3.1"
            assert "huggingface" in updated.provenance.get("sources", [])
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_repair_removes_unmatched_sources_from_provenance(
        self,
    ) -> None:
        """A legacy row whose provenance claims Hugging Face but has
        no persisted HF observation should have the source removed
        from provenance during repair."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-4o")

            repo = ModelInfoRepository(db)
            now = datetime.now(UTC)
            legacy_row = CanonicalModelInfo(
                model_id="gpt-4o",
                status="partial",
                summary="legacy",
                sparse=False,
                detail={
                    "providers": ["openai"],
                    "context_tokens": 128000,
                },
                # Pre-Phase-B provenance over-claimed sources even when
                # no observation rows existed.
                provenance={
                    "sources": [
                        "provider_catalog",
                        "openrouter",
                        "huggingface",
                        "artificial_analysis",
                    ],
                    "reconciled_at": (now - timedelta(days=1)).isoformat(),
                },
                conflicts={},
                first_seen_at=now - timedelta(days=2),
                last_seen_at=now - timedelta(hours=1),
                last_refreshed_at=now - timedelta(hours=1),
                next_refresh_at=now + timedelta(hours=1),
            )
            await repo.upsert_canonical(legacy_row)

            cache = _make_cache("gpt-4o")
            service = ModelInfoService(ModelInfoConfig(), db, cache)
            result = await service.backfill_legacy_detail_blocks()
            assert result["upgraded"] == 1

            updated = await repo.get_canonical("gpt-4o")
            assert updated is not None
            # Only sources with persisted observations appear in
            # provenance. With no observations on disk, only the
            # provider_catalog source survives.
            assert "huggingface" not in updated.provenance["sources"]
            assert "openrouter" not in updated.provenance["sources"]
            assert "artificial_analysis" not in updated.provenance["sources"]
            assert "provider_catalog" in updated.provenance["sources"]
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_repair_is_idempotent(self) -> None:
        """Running the repair twice produces the same upgraded row
        without re-upgrading it (the second run reports
        ``upgraded=0``)."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-4o")

            repo = ModelInfoRepository(db)
            await repo.upsert_canonical(_legacy_canonical("gpt-4o"))

            cache = _make_cache("gpt-4o")
            service = ModelInfoService(ModelInfoConfig(), db, cache)

            first = await service.backfill_legacy_detail_blocks()
            assert first["upgraded"] == 1

            second = await service.backfill_legacy_detail_blocks()
            assert second["upgraded"] == 0
            assert second["skipped"] == 1
        finally:
            await db.disconnect()
