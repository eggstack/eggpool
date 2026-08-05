"""Tests for Phase 5: model-info health snapshot.

Covers ``ModelInfoService.health_snapshot`` for:
- Disabled config returning ``{enabled: False}``.
- Zero canonical rows returning ``canonical_count=0``.
- Source health present without raw payloads.
- Missing ``count_canonical`` returning an error key.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eggpool.catalog.cache import ModelCatalogCache
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.model_info.repository import ModelInfoRepository
from eggpool.model_info.service import ModelInfoService
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


def _make_cache() -> ModelCatalogCache:
    cache = ModelCatalogCache()
    now_ts = datetime.now(UTC).timestamp()
    cache._models["gpt-4o"] = {
        "model_id": "gpt-4o",
        "display_name": "GPT-4o",
        "protocol": "openai",
        "capabilities": {},
        "source_metadata": {},
        "first_seen_at": now_ts,
        "last_seen_at": now_ts,
        "discovered_limits": {},
        "effective_limits": {"context_tokens": 128000},
    }
    cache._provider_models[("gpt-4o", "openai")] = dict(cache._models["gpt-4o"])
    return cache


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_health_snapshot_disabled_when_disabled_config() -> None:
    """Service with ``config.enabled=False`` returns ``{enabled: False}``."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        cache = _make_cache()
        config = ModelInfoConfig(enabled=False)
        service = ModelInfoService(config, db, cache)

        snapshot = await service.health_snapshot()
        assert snapshot["enabled"] is False
        assert "error" in snapshot
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_health_snapshot_present_with_zero_canonical_returns_zero() -> None:
    """Empty repo -> canonical_count=0, no exception."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        cache = _make_cache()
        config = ModelInfoConfig(enabled=True)
        service = ModelInfoService(config, db, cache)

        snapshot = await service.health_snapshot()
        assert snapshot["enabled"] is True
        assert snapshot["canonical_count"] == 0
        assert snapshot["catalog_model_count"] == 1
        assert snapshot["provider_model_count"] == 1
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_health_snapshot_with_canonical_rows() -> None:
    """Canonical rows are counted correctly."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _seed_model(db, "gpt-4o")

        cache = _make_cache()
        config = ModelInfoConfig(enabled=True)
        service = ModelInfoService(config, db, cache)
        await service.reconcile_catalog_snapshot(reason="test")

        snapshot = await service.health_snapshot()
        assert snapshot["enabled"] is True
        assert snapshot["canonical_count"] >= 1
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_health_snapshot_source_health_present_no_raw_payload() -> None:
    """With sources populated, payload has source_health dict without raw_json key."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _seed_model(db, "gpt-4o")

        cache = _make_cache()
        config = ModelInfoConfig(enabled=True)
        service = ModelInfoService(config, db, cache)

        # Insert a source_health row via record_source_success
        repo = ModelInfoRepository(db)
        await repo.record_source_success(
            "openrouter",
            status_code=200,
            payload_count=10,
        )

        snapshot = await service.health_snapshot()
        assert "source_health" in snapshot
        health = snapshot["source_health"]
        assert "openrouter" in health
        openrouter = health["openrouter"]
        assert openrouter["enabled"] is True
        # No raw_json key should be present
        assert "raw_json" not in openrouter
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_health_snapshot_handles_missing_count_call() -> None:
    """If ``count_canonical`` raises, payload includes ``canonical_count_error``."""

    class _FailingRepo(ModelInfoRepository):
        async def count_canonical(self) -> int:  # type: ignore[override]
            raise RuntimeError("db gone")

    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        cache = _make_cache()
        config = ModelInfoConfig(enabled=True)

        service = ModelInfoService(config, db, cache)
        # Swap in the failing repo
        service._repo = _FailingRepo(db)  # type: ignore[assignment]

        snapshot = await service.health_snapshot()
        assert snapshot["enabled"] is True
        assert "canonical_count_error" in snapshot
        assert snapshot["canonical_count_error"] == "RuntimeError"
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_health_snapshot_due_count_present() -> None:
    """due_count is included in the snapshot."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        cache = _make_cache()
        config = ModelInfoConfig(enabled=True)
        service = ModelInfoService(config, db, cache)

        snapshot = await service.health_snapshot()
        assert "due_count" in snapshot
        assert isinstance(snapshot["due_count"], int)
    finally:
        await db.disconnect()
