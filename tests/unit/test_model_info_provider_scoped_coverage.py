"""Tests for Phase 3+4: provider-scoped model-info coverage.

Covers:
- ``get_summary_map`` includes ``_provider_models`` keys.
- ``ensure_canonical`` marks ``in_catalog=True`` for provider-only models.
- ``reconcile_catalog_snapshot`` covers both ``_models`` and
  ``_provider_models`` keys.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eggpool.catalog.cache import ModelCatalogCache
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
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


def _make_cache(
    models: dict[str, dict] | None = None,
    provider_models: dict[tuple[str, str], dict] | None = None,
) -> ModelCatalogCache:
    cache = ModelCatalogCache()
    now_ts = datetime.now(UTC).timestamp()
    if models:
        for mid, info in models.items():
            entry = {
                "model_id": mid,
                "display_name": info.get("display_name", mid),
                "protocol": info.get("protocol", "openai"),
                "capabilities": info.get("capabilities", {}),
                "source_metadata": {},
                "first_seen_at": now_ts,
                "last_seen_at": now_ts,
                "discovered_limits": {},
                "effective_limits": info.get(
                    "effective_limits",
                    {
                        "context_tokens": 128000,
                        "input_tokens": 128000,
                        "output_tokens": 16384,
                    },
                ),
            }
            cache._models[mid] = entry
    if provider_models:
        for (mid, pid), info in provider_models.items():
            entry = {
                "model_id": mid,
                "display_name": info.get("display_name", mid),
                "protocol": info.get("protocol", "openai"),
                "capabilities": info.get("capabilities", {}),
                "source_metadata": {},
                "first_seen_at": now_ts,
                "last_seen_at": now_ts,
                "discovered_limits": {},
                "effective_limits": info.get(
                    "effective_limits",
                    {
                        "context_tokens": 128000,
                        "input_tokens": 128000,
                        "output_tokens": 16384,
                    },
                ),
            }
            cache._provider_models[(mid, pid)] = entry
    return cache


# ---------------------------------------------------------------------------
# get_summary_map includes provider_model keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_get_summary_map_includes_provider_model_keys() -> None:
    """Service with only ``_provider_models`` entry returns that model_id."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _seed_model(db, "gpt-4o")

        cache = _make_cache(
            provider_models={
                ("gpt-4o", "openai"): {"protocol": "openai"},
            }
        )
        config = ModelInfoConfig()
        service = ModelInfoService(config, db, cache)

        # Reconcile so a canonical row exists
        await service.reconcile_catalog_snapshot(reason="test")

        # get_summary_map without explicit ids should include gpt-4o
        result = await service.get_summary_map()
        assert "gpt-4o" in result
    finally:
        await db.disconnect()


# ---------------------------------------------------------------------------
# ensure_canonical uses provider_models
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_ensure_canonical_uses_provider_models() -> None:
    """ensure_canonical marks in_catalog=True for provider-only models."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)

        cache = _make_cache(
            provider_models={
                ("gpt-4o", "openai"): {
                    "protocol": "openai",
                    "capabilities": {"supports_tools": True},
                    "effective_limits": {
                        "context_tokens": 128000,
                        "input_tokens": 128000,
                        "output_tokens": 16384,
                        "enforce": True,
                    },
                },
            }
        )
        config = ModelInfoConfig()
        service = ModelInfoService(config, db, cache)

        # Model exists only in _provider_models, not in _models
        assert "gpt-4o" not in cache._models

        info = await service.ensure_canonical("gpt-4o")
        assert info is not None
        assert info.status == "partial"
        assert info.sparse is False
        assert "provider_catalog" in info.provenance.get("sources", [])
    finally:
        await db.disconnect()


# ---------------------------------------------------------------------------
# reconcile_catalog_snapshot covers provider_models
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_reconcile_catalog_snapshot_covers_provider_models() -> None:
    """Service reconciles both ``_models`` and ``_provider_models`` keys."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _seed_model(db, "gpt-4o")
        await _seed_model(db, "llama-3-8b")

        cache = _make_cache(
            models={"llama-3-8b": {"protocol": "openai"}},
            provider_models={
                ("gpt-4o", "openai"): {"protocol": "openai"},
            },
        )
        config = ModelInfoConfig()
        service = ModelInfoService(config, db, cache)

        result = await service.reconcile_catalog_snapshot(reason="test")
        # Both gpt-4o (provider-only) and llama-3-8b (models) should get rows.
        assert result["created"] >= 2

        gpt_info = await service.get_summary("gpt-4o")
        assert gpt_info is not None

        llama_info = await service.get_summary("llama-3-8b")
        assert llama_info is not None
    finally:
        await db.disconnect()


# ---------------------------------------------------------------------------
# get_summary_map with explicit ids from provider_models
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_get_summary_map_explicit_ids_from_provider_models() -> None:
    """Explicit model_ids from ``_provider_models`` are resolved."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _seed_model(db, "gpt-4o")

        cache = _make_cache(
            provider_models={
                ("gpt-4o", "openai"): {"protocol": "openai"},
            }
        )
        config = ModelInfoConfig()
        service = ModelInfoService(config, db, cache)
        await service.reconcile_catalog_snapshot(reason="test")

        result = await service.get_summary_map(model_ids=["gpt-4o"])
        assert "gpt-4o" in result
    finally:
        await db.disconnect()
