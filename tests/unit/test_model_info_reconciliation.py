"""Tests for the canonical detail builder and non-destructive reconcile.

Phase B of the model-info corrective plan:
- ``build_canonical_detail`` merges provider-native detail with
  external observation payloads, preserves previously known external
  fields, and produces a clean provenance ``sources`` list.
- ``reconcile_catalog_snapshot`` does not wipe previously persisted
  external data on restart.
- Hugging Face observation rows are reflected in the canonical detail.
- OpenRouter observation rows populate ``external_ids`` and
  ``external_context`` in the normalized limits block.
- Provenance ``sources`` only includes sources that contributed a
  matched observation (not the configured adapter set).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eggpool.catalog.cache import ModelCatalogCache
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.model_info.repository import ModelInfoRepository
from eggpool.model_info.service import (
    ModelInfoService,
    build_canonical_detail,
)
from eggpool.model_info.types import (
    BenchmarkObservation,
    SourceModelRecord,
)
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
        "capabilities": {"supports_tools": True, "supports_vision": False},
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


def _make_record(
    *,
    source: str,
    source_model_id: str,
    context_window: int | None = None,
    max_output_tokens: int | None = None,
    license_value: str | None = None,
    benchmarks: tuple[BenchmarkObservation, ...] = (),
    normalized: dict[str, object] | None = None,
) -> SourceModelRecord:
    return SourceModelRecord(
        source=source,
        source_model_id=source_model_id,
        observed_at=datetime.now(UTC),
        raw_hash=f"hash-{source_model_id}",
        raw_payload={},
        normalized=normalized or {},
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        license=license_value,
        benchmarks=benchmarks,
        confidence=0.9,
    )


# ---------------------------------------------------------------------------
# Pure-function tests for build_canonical_detail
# ---------------------------------------------------------------------------


class TestBuildCanonicalDetail:
    def test_provider_detail_and_external_observations_merge_into_normalized_limits(
        self,
    ) -> None:
        provider_detail = {
            "limits": {
                "effective_context": 220000,
                "effective_input": 220000,
                "effective_output": 8192,
            },
            "providers": ["openai"],
            "supports_tools": True,
        }
        # External value within 10% of effective so no conflict fires.
        or_record = _make_record(
            source="openrouter",
            source_model_id="openai/gpt-x",
            context_window=200000,
        )
        observation_payloads = [
            {
                "source": "openrouter",
                "source_model_id": or_record.source_model_id,
                "observed_at": or_record.observed_at.isoformat(),
                "confidence": 0.9,
                "normalized": {
                    "context_window": or_record.context_window,
                    "max_output_tokens": None,
                    "modalities": ["text"],
                    "display_name": "GPT-X",
                },
            }
        ]
        detail, provenance, conflicts = build_canonical_detail(
            model_id="gpt-x",
            provider_detail=provider_detail,
            observation_payloads=observation_payloads,
        )
        limits = detail["limits"]
        assert limits["effective_context"] == 220000
        assert limits["external_context"] == 200000
        # Provider values for tools/providers are preserved.
        assert detail["supports_tools"] is True
        assert detail["providers"] == ["openai"]
        # Modalities are merged into the union.
        assert "text" in detail["modalities"]
        # Sources used in provenance.
        assert "provider_catalog" in provenance["sources"]
        assert "openrouter" in provenance["sources"]
        # No conflict when values are within 10%.
        assert "context_window" not in conflicts

    def test_huggingface_observation_populates_huggingface_metadata(self) -> None:
        payload = {
            "source": "huggingface",
            "source_model_id": "meta-llama/Llama-3-8B",
            "observed_at": datetime.now(UTC).isoformat(),
            "confidence": 0.6,
            "normalized": {
                "pipeline_tag": "text-generation",
                "library_name": "transformers",
                "model_type": "llama",
                "tags": ["llama", "8b"],
                "downloads": 12345,
                "likes": 678,
                "license": "llama3",
            },
        }
        detail, provenance, _conflicts = build_canonical_detail(
            model_id="llama-3-8b",
            provider_detail={},
            observation_payloads=[payload],
        )
        assert "huggingface_metadata" in detail
        hf = detail["huggingface_metadata"]
        assert hf["pipeline_tag"] == "text-generation"
        assert hf["library_name"] == "transformers"
        assert hf["model_type"] == "llama"
        assert hf["downloads"] == 12345
        assert hf["license"] == "llama3"
        assert "huggingface" in provenance["sources"]
        # No provider detail → no provider_catalog in sources.
        assert "provider_catalog" not in provenance["sources"]

    def test_openrouter_observation_populates_external_ids_and_external_context(
        self,
    ) -> None:
        payload = {
            "source": "openrouter",
            "source_model_id": "openai/gpt-4o",
            "observed_at": datetime.now(UTC).isoformat(),
            "confidence": 1.0,
            "normalized": {
                "context_window": 128000,
                "modalities": ["text", "image"],
                "display_name": "GPT-4o",
            },
        }
        detail, _, _conflicts = build_canonical_detail(
            model_id="gpt-4o",
            provider_detail={"limits": {"effective_context": 128000}},
            observation_payloads=[payload],
        )
        assert detail["external_ids"]["openrouter"] == "openai/gpt-4o"
        assert detail["limits"]["external_context"] == 128000
        assert "text" in detail["modalities"]
        assert "image" in detail["modalities"]

    def test_provenance_sources_only_include_used_observations(self) -> None:
        # Provider-only, no observations: only provider_catalog.
        detail, prov, _ = build_canonical_detail(
            model_id="x",
            provider_detail={"providers": ["a"]},
            observation_payloads=[],
        )
        assert prov["sources"] == ["provider_catalog"]

        # Observation that contributes nothing (empty normalized) is
        # filtered out.
        detail, prov, _ = build_canonical_detail(
            model_id="x",
            provider_detail={"providers": ["a"]},
            observation_payloads=[
                {
                    "source": "openrouter",
                    "source_model_id": "x",
                    "normalized": {},
                }
            ],
        )
        assert prov["sources"] == ["provider_catalog"]

    def test_existing_detail_preserves_external_fields_on_failure(
        self,
    ) -> None:
        """If a cycle's external fetch fails, the previous Hugging Face
        metadata and benchmarks must not be wiped."""
        existing = {
            "limits": {
                "effective_context": 128000,
                "external_context": 1_000_000,
            },
            "huggingface_metadata": {
                "pipeline_tag": "text-generation",
                "library_name": "transformers",
                "license": "apache-2.0",
            },
            "benchmarks": [
                {"name": "MMLU", "score": 0.75, "source": "artificial_analysis"}
            ],
            "external_ids": {"openrouter": "openai/gpt-4o"},
        }
        # New cycle: provider context stays; no external observations.
        detail, prov, _ = build_canonical_detail(
            model_id="gpt-4o",
            provider_detail={
                "limits": {"effective_context": 128000},
            },
            observation_payloads=[],
            existing_detail=existing,
        )
        # Previous external fields are preserved.
        assert detail["limits"]["external_context"] == 1_000_000
        assert detail["huggingface_metadata"]["pipeline_tag"] == "text-generation"
        assert detail["huggingface_metadata"]["license"] == "apache-2.0"
        assert any(b.get("name") == "MMLU" for b in detail["benchmarks"])
        # No external sources this cycle.
        assert prov["sources"] == ["provider_catalog"]

    def test_conflict_recorded_when_external_differs_materially(self) -> None:
        provider_detail = {"limits": {"effective_context": 128000}}
        payload = {
            "source": "openrouter",
            "source_model_id": "openai/gpt-4o",
            "normalized": {"context_window": 1_000_000},
        }
        detail, _prov, conflicts = build_canonical_detail(
            model_id="gpt-4o",
            provider_detail=provider_detail,
            observation_payloads=[payload],
        )
        assert "context_window" in conflicts
        assert conflicts["context_window"]["provider_catalog"] == 128000
        # The conflict names the source that contributed the external
        # value.
        assert conflicts["context_window"]["openrouter"] == 1_000_000
        assert conflicts["context_window"]["selected"] == (
            "provider_catalog/effective_limit"
        )

    def test_provider_native_fields_never_overwritten(self) -> None:
        provider_detail = {
            "display_name": "Provider Display",
            "limits": {"effective_context": 128000, "effective_output": 8192},
            "supports_tools": True,
            "protocol": "openai",
        }
        payload = {
            "source": "openrouter",
            "source_model_id": "x/y",
            "normalized": {
                "context_window": 999_999,
                "display_name": "OR Display",
            },
        }
        detail, _, _ = build_canonical_detail(
            model_id="x",
            provider_detail=provider_detail,
            observation_payloads=[payload],
        )
        assert detail["display_name"] == "Provider Display"
        assert detail["supports_tools"] is True
        assert detail["protocol"] == "openai"
        # External context enriches limits but does not change effective.
        assert detail["limits"]["effective_context"] == 128000
        assert detail["limits"]["external_context"] == 999_999

    def test_benchmarks_merged_from_artificial_analysis(self) -> None:
        payload = {
            "source": "artificial_analysis",
            "source_model_id": "gpt-4o",
            "normalized": {
                "display_name": "GPT-4o (AA)",
                "benchmarks": [
                    {
                        "name": "MMLU",
                        "score": 0.88,
                        "source": "artificial_analysis",
                    }
                ],
            },
        }
        detail, prov, _ = build_canonical_detail(
            model_id="gpt-4o",
            provider_detail={"providers": ["openai"]},
            observation_payloads=[payload],
        )
        assert len(detail["benchmarks"]) == 1
        assert detail["benchmarks"][0]["name"] == "MMLU"
        assert "artificial_analysis" in prov["sources"]


# ---------------------------------------------------------------------------
# Integration: reconcile_catalog_snapshot does not wipe external data
# ---------------------------------------------------------------------------


class TestReconcilePreservesObservations:
    @pytest.mark.asyncio()
    async def test_startup_reconcile_preserves_existing_huggingface_metadata(
        self,
    ) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "llama-3-8b")

            repo = ModelInfoRepository(db)
            # Persist a Hugging Face observation row directly. License
            # and other typed fields live inside the normalized dict
            # so they survive the round-trip through the DB.
            hf_record = _make_record(
                source="huggingface",
                source_model_id="meta-llama/Llama-3-8B",
                license_value="apache-2.0",
                normalized={
                    "pipeline_tag": "text-generation",
                    "library_name": "transformers",
                    "model_type": "llama",
                    "tags": ["llama", "8b"],
                    "downloads": 12345,
                    "license": "apache-2.0",
                },
            )
            await repo.upsert_observation(
                hf_record, model_id="llama-3-8b", provider_id="openai"
            )

            # Run reconcile_catalog_snapshot.
            cache = _make_cache("llama-3-8b")
            service = ModelInfoService(config=ModelInfoConfig(), db=db, catalog=cache)
            await service.reconcile_catalog_snapshot(reason="test")

            # The HF observation must still be reflected in the
            # canonical detail.
            info = await repo.get_canonical("llama-3-8b")
            assert info is not None
            assert "huggingface_metadata" in info.detail
            hf = info.detail["huggingface_metadata"]
            assert hf["pipeline_tag"] == "text-generation"
            assert hf["library_name"] == "transformers"
            assert hf["license"] == "apache-2.0"
            assert hf["downloads"] == 12345
            assert "huggingface" in info.provenance["sources"]
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_startup_reconcile_preserves_existing_benchmarks(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-4o")

            repo = ModelInfoRepository(db)
            aa_record = _make_record(
                source="artificial_analysis",
                source_model_id="gpt-4o",
                benchmarks=(
                    BenchmarkObservation(
                        benchmark_name="MMLU",
                        score=0.88,
                        source="artificial_analysis",
                    ),
                ),
                normalized={
                    "display_name": "GPT-4o (AA)",
                    "benchmarks": [
                        {
                            "name": "MMLU",
                            "score": 0.88,
                            "source": "artificial_analysis",
                        }
                    ],
                },
            )
            await repo.upsert_observation(
                aa_record, model_id="gpt-4o", provider_id="openai"
            )

            cache = _make_cache("gpt-4o")
            service = ModelInfoService(config=ModelInfoConfig(), db=db, catalog=cache)
            await service.reconcile_catalog_snapshot(reason="test")

            info = await repo.get_canonical("gpt-4o")
            assert info is not None
            assert info.detail.get("benchmarks"), "AA benchmarks lost"
            assert info.detail["benchmarks"][0]["name"] == "MMLU"
            assert "artificial_analysis" in info.provenance["sources"]
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_reconcile_preserves_existing_when_no_external_observations(
        self,
    ) -> None:
        """If no observation rows exist yet, reconcile still merges
        provider-native detail into the normalized schema."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-4o")

            cache = _make_cache("gpt-4o", context=220000)
            service = ModelInfoService(config=ModelInfoConfig(), db=db, catalog=cache)
            await service.reconcile_catalog_snapshot(reason="test")

            info = await ModelInfoRepository(db).get_canonical("gpt-4o")
            assert info is not None
            limits = info.detail.get("limits", {})
            assert limits.get("effective_context") == 220000
            # Legacy flat key is also present for migration.
            assert info.detail.get("context_tokens") == 220000
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_reconcile_persists_reason_in_provenance(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-4o")

            cache = _make_cache("gpt-4o")
            service = ModelInfoService(config=ModelInfoConfig(), db=db, catalog=cache)
            await service.reconcile_catalog_snapshot(reason="manual")

            info = await ModelInfoRepository(db).get_canonical("gpt-4o")
            assert info is not None
            assert info.provenance.get("reason") == "manual"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_reconcile_does_not_wipe_hf_metadata_on_idempotent_run(
        self,
    ) -> None:
        """Two consecutive reconcile runs with the same data must not
        delete the HF metadata that was persisted by an earlier
        enrichment cycle."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "llama-3-8b")

            repo = ModelInfoRepository(db)
            hf_record = _make_record(
                source="huggingface",
                source_model_id="meta-llama/Llama-3-8B",
                license_value="apache-2.0",
                normalized={
                    "pipeline_tag": "text-generation",
                    "license": "apache-2.0",
                },
            )
            await repo.upsert_observation(
                hf_record, model_id="llama-3-8b", provider_id="openai"
            )

            cache = _make_cache("llama-3-8b")
            service = ModelInfoService(config=ModelInfoConfig(), db=db, catalog=cache)
            # First run
            await service.reconcile_catalog_snapshot(reason="startup")
            # Second run (e.g. another restart)
            await service.reconcile_catalog_snapshot(reason="startup")

            info = await repo.get_canonical("llama-3-8b")
            assert info is not None
            assert "huggingface_metadata" in info.detail
            assert info.detail["huggingface_metadata"]["license"] == "apache-2.0"
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Repository: get_latest_observations_for_model
# ---------------------------------------------------------------------------


class TestLatestObservations:
    @pytest.mark.asyncio()
    async def test_returns_latest_per_source(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-4o")

            repo = ModelInfoRepository(db)
            now = datetime.now(UTC)
            # Insert an older observation, then a newer one. Because
            # the unique key is (source, source_model_id, raw_hash)
            # both rows need different raw_hash values to coexist.
            older = SourceModelRecord(
                source="openrouter",
                source_model_id="openai/gpt-4o",
                observed_at=now - timedelta(hours=2),
                raw_hash="older-hash",
                raw_payload={},
                normalized={},
                context_window=128000,
            )
            await repo.upsert_observation(
                older, model_id="gpt-4o", provider_id="openai"
            )
            newer = SourceModelRecord(
                source="openrouter",
                source_model_id="openai/gpt-4o",
                observed_at=now,
                raw_hash="newer-hash",
                raw_payload={},
                normalized={},
                context_window=256000,
            )
            await repo.upsert_observation(
                newer, model_id="gpt-4o", provider_id="openai"
            )

            latest = await repo.get_latest_observations_for_model("gpt-4o")
            assert "openrouter" in latest
            row = latest["openrouter"]
            assert row["raw_hash"] == "newer-hash"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_filters_by_source_list(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-4o")

            repo = ModelInfoRepository(db)
            await repo.upsert_observation(
                _make_record(
                    source="openrouter",
                    source_model_id="openai/gpt-4o",
                ),
                model_id="gpt-4o",
                provider_id="openai",
            )
            await repo.upsert_observation(
                _make_record(
                    source="huggingface",
                    source_model_id="openai/gpt-4o",
                ),
                model_id="gpt-4o",
                provider_id="openai",
            )
            latest = await repo.get_latest_observations_for_model(
                "gpt-4o", sources=["openrouter"]
            )
            assert "openrouter" in latest
            assert "huggingface" not in latest
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_get_latest_observation_payloads_returns_decoded(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-4o")

            repo = ModelInfoRepository(db)
            await repo.upsert_observation(
                _make_record(
                    source="huggingface",
                    source_model_id="meta-llama/Llama-3-8B",
                    normalized={"pipeline_tag": "text-generation"},
                ),
                model_id="gpt-4o",
                provider_id="openai",
            )
            payloads = await repo.get_latest_observation_payloads("gpt-4o")
            assert any(p["source"] == "huggingface" for p in payloads)
            hf = next(p for p in payloads if p["source"] == "huggingface")
            assert hf["normalized"]["pipeline_tag"] == "text-generation"
        finally:
            await db.disconnect()
