"""Tests for the model-info subsystem."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.model_info.repository import ModelInfoRepository
from eggpool.model_info.service import ModelInfoService
from eggpool.model_info.sources.provider_catalog import ProviderCatalogSource
from eggpool.model_info.types import (
    CanonicalModelInfo,
    SourceModelRecord,
)
from eggpool.models.config import (
    AppConfig,
    ModelInfoConfig,
    ModelInfoSourceConfig,
)


async def _run_migrations(db: Database) -> None:
    runner = MigrationRunner(db)
    await runner.run()


async def _seed_model(
    db: Database, model_id: str = "gpt-4o", display_name: str = "GPT-4o"
) -> None:
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO models (model_id, display_name) VALUES (?, ?)",
            (model_id, display_name),
        )


# --- Migration tests ---


@pytest.mark.asyncio()
async def test_model_info_migration_creates_tables() -> None:
    """Migration 0036 creates the model-info sidecar tables."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)

        rows = await db.fetch_all(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' "
            "AND name LIKE 'model_info_%'"
        )
        table_names = {row["name"] for row in rows}
        assert "model_info_canonical" in table_names
        assert "model_info_observations" in table_names
        assert "model_info_aliases" in table_names
        assert "model_info_source_health" in table_names
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_model_info_migration_is_idempotent() -> None:
    """Running migration twice does not fail or duplicate tables."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _run_migrations(db)

        rows = await db.fetch_all(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name LIKE 'model_info_%'"
        )
        assert (
            len(rows) == 5
        )  # canonical, observations, aliases, source_health, overrides
    finally:
        await db.disconnect()


# --- Repository tests ---


@pytest.mark.asyncio()
async def test_model_info_repository_upserts_canonical() -> None:
    """Repository can insert and update canonical records."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _seed_model(db, "test-model")

        repo = ModelInfoRepository(db)
        now = datetime.now(UTC)
        info = CanonicalModelInfo(
            model_id="test-model",
            status="partial",
            summary="Test summary",
            sparse=False,
            detail={"protocol": "openai"},
            provenance={"sources": ["provider_catalog"]},
            conflicts={},
            first_seen_at=now,
            last_seen_at=now,
            last_refreshed_at=now,
            next_refresh_at=now + timedelta(hours=1),
        )

        await repo.upsert_canonical(info)
        result = await repo.get_canonical("test-model")

        assert result is not None
        assert result.model_id == "test-model"
        assert result.status == "partial"
        assert result.summary == "Test summary"
        assert result.detail == {"protocol": "openai"}

        # Update
        updated = CanonicalModelInfo(
            model_id="test-model",
            status="fresh",
            summary="Updated summary",
            sparse=False,
            detail={"protocol": "openai", "display_name": "GPT-4o"},
            provenance={"sources": ["provider_catalog"]},
            conflicts={},
            first_seen_at=now,
            last_seen_at=now + timedelta(minutes=5),
            last_refreshed_at=now + timedelta(minutes=5),
            next_refresh_at=now + timedelta(hours=2),
        )
        await repo.upsert_canonical(updated)
        result2 = await repo.get_canonical("test-model")
        assert result2 is not None
        assert result2.status == "fresh"
        assert result2.summary == "Updated summary"
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_model_info_repository_deduplicates_observations_by_hash() -> None:
    """Observations are deduplicated by (source, source_model_id, raw_hash)."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _seed_model(db, "test-model")

        repo = ModelInfoRepository(db)
        now = datetime.now(UTC)
        record = SourceModelRecord(
            source="test_source",
            source_model_id="test-source-model",
            observed_at=now,
            raw_hash="abc123",
            raw_payload={"key": "value"},
            normalized={"normalized_key": "norm_value"},
            model_id="test-model",
            provider_id="test_provider",
        )

        # Insert first time
        row_id1 = await repo.upsert_observation(record)
        # Insert same hash again - should update, not duplicate
        row_id2 = await repo.upsert_observation(record)

        rows = await db.fetch_all(
            "SELECT * FROM model_info_observations "
            "WHERE source = 'test_source' AND source_model_id = 'test-source-model'"
        )
        assert len(rows) == 1
        assert row_id1 == row_id2
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_model_info_repository_lists_due_rows() -> None:
    """list_due returns rows where next_refresh_at is past."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _seed_model(db, "model-a")
        await _seed_model(db, "model-b")

        repo = ModelInfoRepository(db)
        now = datetime.now(UTC)

        # model-a is due (next_refresh in the past)
        info_a = CanonicalModelInfo(
            model_id="model-a",
            status="sparse_new",
            summary="A",
            sparse=True,
            detail={},
            provenance={},
            conflicts={},
            first_seen_at=now,
            last_seen_at=now,
            last_refreshed_at=now,
            next_refresh_at=now - timedelta(hours=1),
        )
        await repo.upsert_canonical(info_a)

        # model-b is not due (next_refresh in the future)
        info_b = CanonicalModelInfo(
            model_id="model-b",
            status="partial",
            summary="B",
            sparse=False,
            detail={},
            provenance={},
            conflicts={},
            first_seen_at=now,
            last_seen_at=now,
            last_refreshed_at=now,
            next_refresh_at=now + timedelta(hours=1),
        )
        await repo.upsert_canonical(info_b)

        due = await repo.list_due(limit=10, now=now)
        assert len(due) == 1
        assert due[0].model_id == "model-a"
    finally:
        await db.disconnect()


# --- Config tests ---


def test_model_info_config_defaults() -> None:
    """ModelInfoConfig has safe defaults."""
    config = ModelInfoConfig()
    assert config.enabled is True
    assert config.startup_refresh is True
    assert config.refresh_interval_s == 21_600
    assert config.known_ttl_s == 86_400
    assert config.partial_ttl_s == 43_200
    assert config.sparse_new_initial_ttl_s == 3_600
    assert config.sparse_new_later_ttl_s == 21_600
    assert config.sparse_new_accelerated_days == 7
    assert config.conflict_ttl_s == 7_200
    assert config.max_models_per_cycle == 50
    assert config.include_in_models_endpoint is True
    assert config.store_raw_observations is True


def test_model_info_source_api_key_env_resolution() -> None:
    """ModelInfoSourceConfig resolves api_key from env."""
    import os

    os.environ["TEST_MODEL_INFO_KEY"] = "secret-key-123"
    try:
        source = ModelInfoSourceConfig(api_key_env="TEST_MODEL_INFO_KEY")
        assert source.resolved_api_key == "secret-key-123"
    finally:
        del os.environ["TEST_MODEL_INFO_KEY"]

    source_no_env = ModelInfoSourceConfig(api_key_env="NONEXISTENT_VAR")
    assert source_no_env.resolved_api_key is None

    source_direct = ModelInfoSourceConfig(api_key="direct-key")
    assert source_direct.resolved_api_key == "direct-key"


def test_model_info_rejects_unknown_config_keys() -> None:
    """ModelInfoConfig rejects unknown keys."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelInfoConfig(unknown_field="bad")  # type: ignore[call-arg]


def test_app_config_includes_model_info() -> None:
    """AppConfig includes model_info field with defaults."""
    config = AppConfig(
        server={"host": "127.0.0.1", "port": 9000},
        upstream={"base_url": "https://api.example.com"},
        database={"path": "test.db"},
        accounts=[{"name": "test", "api_key_env": "KEY"}],
    )
    assert hasattr(config, "model_info")
    assert isinstance(config.model_info, ModelInfoConfig)
    assert config.model_info.enabled is True


# --- Source adapter tests ---


@pytest.mark.asyncio()
async def test_provider_catalog_source_emits_observations_from_cache() -> None:
    """ProviderCatalogSource converts cache entries to SourceModelRecords."""
    from eggpool.catalog.cache import ModelCatalogCache

    cache = ModelCatalogCache()
    now = datetime.now(UTC).timestamp()
    cache._models["gpt-4o"] = {
        "model_id": "gpt-4o",
        "display_name": "GPT-4o",
        "protocol": "openai",
        "capabilities": {"supports_tools": True, "supports_vision": False},
        "source_metadata": {},
        "first_seen_at": now,
        "last_seen_at": now,
        "discovered_limits": {},
        "effective_limits": {
            "context_tokens": 128000,
            "input_tokens": 128000,
            "output_tokens": 16384,
            "enforce": True,
        },
    }
    cache._provider_models[("gpt-4o", "openai-provider")] = dict(
        cache._models["gpt-4o"]
    )

    source = ProviderCatalogSource(cache)
    records = await source.fetch_all()

    assert len(records) == 1
    record = records[0]
    assert record.source == "provider_catalog"
    assert record.model_id == "gpt-4o"
    assert record.provider_id == "openai-provider"
    assert record.display_name == "GPT-4o"
    assert record.context_window == 128000
    assert record.supports_tools is True
    assert record.sparse is True
    assert record.confidence == 1.0


@pytest.mark.asyncio()
async def test_provider_catalog_source_fetch_one() -> None:
    """ProviderCatalogSource.fetch_one returns a single record."""
    from eggpool.catalog.cache import ModelCatalogCache

    cache = ModelCatalogCache()
    now = datetime.now(UTC).timestamp()
    cache._models["model-a"] = {
        "model_id": "model-a",
        "display_name": "A",
        "capabilities": {},
        "source_metadata": {},
        "first_seen_at": now,
        "last_seen_at": now,
        "discovered_limits": {},
        "effective_limits": {},
    }
    cache._provider_models[("model-a", "prov1")] = dict(cache._models["model-a"])
    cache._models["model-b"] = {
        "model_id": "model-b",
        "display_name": "B",
        "capabilities": {},
        "source_metadata": {},
        "first_seen_at": now,
        "last_seen_at": now,
        "discovered_limits": {},
        "effective_limits": {},
    }
    cache._provider_models[("model-b", "prov1")] = dict(cache._models["model-b"])

    source = ProviderCatalogSource(cache)
    result = await source.fetch_one("model-a")
    assert result is not None
    assert result.model_id == "model-a"

    result_none = await source.fetch_one("nonexistent")
    assert result_none is None


# --- Service tests ---


@pytest.mark.asyncio()
async def test_reconcile_catalog_snapshot_creates_sparse_rows() -> None:
    """Reconcile creates sparse_new rows for models with minimal metadata."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _seed_model(db, "sparse-model")

        from eggpool.catalog.cache import ModelCatalogCache

        cache = ModelCatalogCache()
        now_ts = datetime.now(UTC).timestamp()
        # Minimal entry: only display_name, no limits, no capabilities
        cache._models["sparse-model"] = {
            "model_id": "sparse-model",
            "display_name": None,
            "protocol": None,
            "capabilities": {},
            "source_metadata": {},
            "first_seen_at": now_ts,
            "last_seen_at": now_ts,
            "discovered_limits": {},
            "effective_limits": {},
        }
        cache._provider_models[("sparse-model", "test-provider")] = dict(
            cache._models["sparse-model"]
        )

        config = ModelInfoConfig()
        service = ModelInfoService(config, db, cache)

        result = await service.reconcile_catalog_snapshot(reason="test")
        assert result["created"] == 1
        assert result["total"] == 1

        info = await service.get_summary("sparse-model")
        assert info is not None
        assert info.status == "sparse_new"
        assert info.sparse is True
        assert "metadata sparse" in (info.summary or "").lower()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_reconcile_creates_partial_for_models_with_context_limit() -> None:
    """Models with context limit but no benchmarks are 'partial'."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _seed_model(db, "partial-model", "Partial Model")

        from eggpool.catalog.cache import ModelCatalogCache

        cache = ModelCatalogCache()
        now_ts = datetime.now(UTC).timestamp()
        cache._models["partial-model"] = {
            "model_id": "partial-model",
            "display_name": "Partial Model",
            "protocol": "openai",
            "capabilities": {"supports_tools": True},
            "source_metadata": {},
            "first_seen_at": now_ts,
            "last_seen_at": now_ts,
            "discovered_limits": {},
            "effective_limits": {
                "context_tokens": 200000,
                "input_tokens": 200000,
                "output_tokens": 32000,
                "enforce": True,
            },
        }
        cache._provider_models[("partial-model", "test-provider")] = dict(
            cache._models["partial-model"]
        )

        config = ModelInfoConfig()
        service = ModelInfoService(config, db, cache)

        result = await service.reconcile_catalog_snapshot(reason="test")
        assert result["created"] == 1

        info = await service.get_summary("partial-model")
        assert info is not None
        assert info.status == "partial"
        assert info.sparse is False
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_summary_mentions_sparse_for_new_sparse_model() -> None:
    """Summary explicitly says metadata is sparse for sparse_new models."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _seed_model(db, "new-model")

        from eggpool.catalog.cache import ModelCatalogCache

        cache = ModelCatalogCache()
        now_ts = datetime.now(UTC).timestamp()
        cache._models["new-model"] = {
            "model_id": "new-model",
            "display_name": None,
            "protocol": None,
            "capabilities": {},
            "source_metadata": {},
            "first_seen_at": now_ts,
            "last_seen_at": now_ts,
            "discovered_limits": {},
            "effective_limits": {},
        }
        cache._provider_models[("new-model", "test-provider")] = dict(
            cache._models["new-model"]
        )

        config = ModelInfoConfig()
        service = ModelInfoService(config, db, cache)
        await service.reconcile_catalog_snapshot(reason="test")

        info = await service.get_summary("new-model")
        assert info is not None
        assert info.summary is not None
        assert "metadata sparse" in info.summary.lower()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_manual_absence_of_external_sources_does_not_fail() -> None:
    """Service works without any external sources configured."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _seed_model(db, "basic-model")

        from eggpool.catalog.cache import ModelCatalogCache

        cache = ModelCatalogCache()
        now_ts = datetime.now(UTC).timestamp()
        cache._models["basic-model"] = {
            "model_id": "basic-model",
            "display_name": "Basic",
            "protocol": "openai",
            "capabilities": {},
            "source_metadata": {},
            "first_seen_at": now_ts,
            "last_seen_at": now_ts,
            "discovered_limits": {},
            "effective_limits": {},
        }
        cache._provider_models[("basic-model", "test-provider")] = dict(
            cache._models["basic-model"]
        )

        config = ModelInfoConfig()
        service = ModelInfoService(config, db, cache)

        # Should not raise even without external sources
        result = await service.reconcile_catalog_snapshot(reason="test")
        assert result["created"] == 1
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_provider_catalog_refresh_and_reconcile_roundtrip() -> None:
    """Full roundtrip: refresh observations then reconcile."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _seed_model(db, "gpt-4o", "GPT-4o")

        from eggpool.catalog.cache import ModelCatalogCache

        cache = ModelCatalogCache()
        now_ts = datetime.now(UTC).timestamp()
        cache._models["gpt-4o"] = {
            "model_id": "gpt-4o",
            "display_name": "GPT-4o",
            "protocol": "openai",
            "capabilities": {"supports_tools": True, "supports_vision": True},
            "source_metadata": {},
            "first_seen_at": now_ts,
            "last_seen_at": now_ts,
            "discovered_limits": {},
            "effective_limits": {
                "context_tokens": 128000,
                "input_tokens": 128000,
                "output_tokens": 16384,
                "enforce": True,
            },
        }
        cache._provider_models[("gpt-4o", "openai-provider")] = dict(
            cache._models["gpt-4o"]
        )

        config = ModelInfoConfig()
        service = ModelInfoService(config, db, cache)

        # Refresh observations
        obs_result = await service.refresh_provider_catalog_observations()
        assert obs_result["observations"] == 1

        # Reconcile
        rec_result = await service.reconcile_catalog_snapshot(reason="test")
        assert rec_result["created"] == 1

        # Verify
        info = await service.get_summary("gpt-4o")
        assert info is not None
        assert info.status == "partial"
        assert info.detail.get("context_tokens") == 128000
        assert info.detail.get("supports_tools") is True
        assert "Callable via openai-provider" in (info.summary or "")
    finally:
        await db.disconnect()


# --- ensure_canonical (lazy backfill for the dashboard detail page) ---


@pytest.mark.asyncio()
async def test_ensure_canonical_creates_unmatched_for_traffic_only_model() -> None:
    """A model that was never catalogued still gets a canonical row."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)

        from eggpool.catalog.cache import ModelCatalogCache

        cache = ModelCatalogCache()
        config = ModelInfoConfig()
        service = ModelInfoService(config, db, cache)

        assert await service.get_summary("traffic-only-model") is None

        info = await service.ensure_canonical("traffic-only-model")

        assert info is not None
        assert info.model_id == "traffic-only-model"
        assert info.status == "unmatched"
        assert info.sparse is True
        assert info.detail == {}
        assert info.provenance["sources"] == ["traffic_observation"]
        assert info.provenance["lazy_created"] is True
        assert info.last_refreshed_at is None
        assert info.next_refresh_at is not None

        # The FK on model_info_canonical was satisfied by seeding a
        # placeholder ``models`` row inside the same transaction.
        async with db.transaction():
            row = await db.fetch_one(
                "SELECT model_id FROM models WHERE model_id = ?",
                ("traffic-only-model",),
            )
        assert row is not None
        assert row["model_id"] == "traffic-only-model"

        # Subsequent lookup reads the persisted row, not a fresh one
        again = await service.ensure_canonical("traffic-only-model")
        assert again.status == "unmatched"
        assert again.detail == {}

        # And get_summary now returns it without further work
        fetched = await service.get_summary("traffic-only-model")
        assert fetched is not None
        assert fetched.model_id == "traffic-only-model"
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_ensure_canonical_picks_up_catalog_detail_when_present() -> None:
    """Models already in the catalog get a catalog-shaped row, not unmatched."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _seed_model(db, "catalog-model", "Catalog Model")

        from eggpool.catalog.cache import ModelCatalogCache

        cache = ModelCatalogCache()
        now_ts = datetime.now(UTC).timestamp()
        cache._models["catalog-model"] = {
            "model_id": "catalog-model",
            "display_name": "Catalog Model",
            "protocol": "openai",
            "capabilities": {"supports_tools": True},
            "source_metadata": {},
            "first_seen_at": now_ts,
            "last_seen_at": now_ts,
            "discovered_limits": {},
            "effective_limits": {
                "context_tokens": 64000,
                "input_tokens": 64000,
                "output_tokens": 8000,
                "enforce": True,
            },
        }
        cache._provider_models[("catalog-model", "demo-provider")] = dict(
            cache._models["catalog-model"]
        )

        config = ModelInfoConfig()
        service = ModelInfoService(config, db, cache)

        info = await service.ensure_canonical("catalog-model")

        assert info.status == "partial"
        assert info.sparse is False
        assert info.provenance["sources"] == ["provider_catalog"]
        assert info.detail.get("display_name") == "Catalog Model"
        assert info.detail.get("context_tokens") == 64000
        assert info.detail.get("supports_tools") is True
        assert "demo-provider" in info.detail.get("providers", [])
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_ensure_canonical_returns_existing_row_without_overwrite() -> None:
    """If a canonical row already exists, ensure_canonical must not touch it."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _seed_model(db, "existing-model")

        from eggpool.catalog.cache import ModelCatalogCache

        cache = ModelCatalogCache()
        now = datetime.now(UTC)
        seeded = CanonicalModelInfo(
            model_id="existing-model",
            status="fresh",
            summary="seeded summary",
            sparse=False,
            detail={"display_name": "Existing"},
            provenance={"sources": ["seed"]},
            conflicts={},
            first_seen_at=now - timedelta(days=2),
            last_seen_at=now - timedelta(hours=1),
            last_refreshed_at=now - timedelta(hours=2),
            next_refresh_at=now + timedelta(hours=20),
        )
        repo = ModelInfoRepository(db)
        await repo.upsert_canonical(seeded)

        config = ModelInfoConfig()
        service = ModelInfoService(config, db, cache)

        info = await service.ensure_canonical("existing-model")
        assert info.summary == "seeded summary"
        assert info.status == "fresh"
        assert info.provenance["sources"] == ["seed"]
        # The lazy-created marker must NOT appear on a pre-existing row
        assert "lazy_created" not in info.provenance
        # first_seen_at is preserved (not bumped to now)
        assert info.first_seen_at == now - timedelta(days=2)
    finally:
        await db.disconnect()


# --- backfill_missing_canonical ---


@pytest.mark.asyncio()
async def test_backfill_missing_canonical_creates_rows_for_orphans() -> None:
    """Models table rows without canonical rows are backfilled."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _seed_model(db, "orphan-a")
        await _seed_model(db, "orphan-b")

        from eggpool.catalog.cache import ModelCatalogCache

        cache = ModelCatalogCache()
        config = ModelInfoConfig()
        service = ModelInfoService(config, db, cache)

        result = await service.backfill_missing_canonical()
        # __deprecated__ + orphan-a + orphan-b = 3
        assert result["backfilled"] == 3

        info_a = await service.get_summary("orphan-a")
        assert info_a is not None
        assert info_a.status == "unmatched"
        assert info_a.provenance["lazy_created"] is True

        info_b = await service.get_summary("orphan-b")
        assert info_b is not None
        assert info_b.status == "unmatched"
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_backfill_missing_canonical_skips_existing_rows() -> None:
    """Models that already have a canonical row are not touched."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)

        from eggpool.catalog.cache import ModelCatalogCache

        cache = ModelCatalogCache()
        config = ModelInfoConfig()
        service = ModelInfoService(config, db, cache)

        # Seed orphan-a and cover it with a canonical row
        await _seed_model(db, "orphan-a")
        await service.ensure_canonical("orphan-a")
        existing = await service.get_summary("orphan-a")
        assert existing is not None
        original_first_seen = existing.first_seen_at

        # backfill should only fill __deprecated__ (the other migration
        # seed), not orphan-a which already has a canonical row
        result = await service.backfill_missing_canonical()
        assert result["backfilled"] == 1

        # Original row must not have been modified
        again = await service.get_summary("orphan-a")
        assert again is not None
        assert again.first_seen_at == original_first_seen
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_backfill_missing_canonical_respects_limit() -> None:
    """Backfill processes at most N models per call."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        for i in range(5):
            await _seed_model(db, f"model-{i}")

        from eggpool.catalog.cache import ModelCatalogCache

        cache = ModelCatalogCache()
        config = ModelInfoConfig()
        service = ModelInfoService(config, db, cache)

        # __deprecated__ + model-0..4 = 6 total without canonical
        result = await service.backfill_missing_canonical(limit=2)
        assert result["backfilled"] == 2

        # Remaining 4 should still be unfilled
        result2 = await service.backfill_missing_canonical(limit=2)
        assert result2["backfilled"] == 2

        result3 = await service.backfill_missing_canonical(limit=2)
        assert result3["backfilled"] == 2

        # All done now
        result4 = await service.backfill_missing_canonical()
        assert result4["backfilled"] == 0
    finally:
        await db.disconnect()
