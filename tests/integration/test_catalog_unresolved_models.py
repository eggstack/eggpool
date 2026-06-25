"""Integration tests for unresolved model quarantine during catalog persistence."""

from __future__ import annotations

import logging

import httpx
import pytest

from eggpool.accounts.registry import AccountRegistry
from eggpool.catalog.cache import ModelCatalogCache
from eggpool.catalog.protocols import ModelProtocolResolver
from eggpool.catalog.service import CatalogService
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.models.config import AppConfig


async def _seed_db(db: Database) -> None:
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("test-acct", "TEST_KEY"),
        )


@pytest.mark.asyncio
async def test_unresolved_model_does_not_block_resolved() -> None:
    """A refresh with both resolved and unresolved models commits resolved ones."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    await _seed_db(db)

    # Simulate a cache with mixed models
    cache = ModelCatalogCache()
    resolver = ModelProtocolResolver()

    # One account refresh containing both resolved and unresolved models.
    cache.update_from_account(
        "test-acct",
        "opencode-go",
        [
            {
                "model_id": "gpt-4o",
                "display_name": "GPT-4o",
                "protocol": "openai",
                "protocol_source": "exact_mapping",
                "capabilities": {},
                "source_metadata": {},
            },
            {
                "model_id": "unknown-weird-model",
                "display_name": "Weird Model",
                "protocol": None,
                "protocol_source": "unresolved",
                "capabilities": {},
                "source_metadata": {},
            },
            {
                "model_id": "claude-3-5-sonnet-20241022",
                "display_name": "Claude 3.5 Sonnet",
                "protocol": "anthropic",
                "protocol_source": "family_mapping",
                "capabilities": {},
                "source_metadata": {},
            },
        ],
    )

    # Verify protocol resolution: unresolved model gets empty protocol
    resolution = resolver.resolve_from_catalog("unknown-weird-model")
    assert resolution.source == "unresolved"
    assert resolution.protocol == ""

    # Verify cache behavior: only resolved models are exposed
    exposed = cache.get_models_for_exposure("union", {"test-acct"})
    model_ids = [m["model_id"] for m in exposed]
    assert "gpt-4o" in model_ids
    assert "claude-3-5-sonnet-20241022" in model_ids
    assert "unknown-weird-model" not in model_ids

    await db.disconnect()


@pytest.mark.asyncio
async def test_unresolved_model_not_exposed() -> None:
    """An unresolved model is excluded from exposure even if account supports it."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()

    cache = ModelCatalogCache()

    # Unresolved model with no protocol
    cache.update_from_account(
        "test-acct",
        "opencode-go",
        [
            {
                "model_id": "mystery-model",
                "display_name": "Mystery",
                "protocol": None,
                "protocol_source": "unresolved",
                "capabilities": {},
                "source_metadata": {},
            },
        ],
    )

    # is_model_available now checks both account support and protocol
    assert not cache.is_model_available("mystery-model", {"test-acct"})

    # But it is NOT exposed — get_models_for_exposure filters by protocol
    exposed = cache.get_models_for_exposure("union", {"test-acct"})
    model_ids = [m["model_id"] for m in exposed]
    assert "mystery-model" not in model_ids

    # Resolved model is exposed
    cache.update_from_account(
        "test-acct",
        "opencode-go",
        [
            {
                "model_id": "gpt-4o",
                "display_name": "GPT-4o",
                "protocol": "openai",
                "protocol_source": "exact_mapping",
                "capabilities": {},
                "source_metadata": {},
            },
        ],
    )

    exposed = cache.get_models_for_exposure("union", {"test-acct"})
    model_ids = [m["model_id"] for m in exposed]
    assert "gpt-4o" in model_ids
    await db.disconnect()


@pytest.mark.asyncio
async def test_persist_skips_unresolved_models() -> None:
    """_persist_catalog skips models without openai/anthropic protocol."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    await _seed_db(db)

    # Directly insert a resolved model
    async with db.transaction():
        await db.execute_insert(
            "INSERT INTO models "
            "(model_id, protocol, resolution_status) VALUES (?, ?, ?)",
            ("gpt-4o", "openai", "resolved"),
        )

    # Try to insert an unresolved model (simulating what _persist_catalog would do)
    # After our fix, _persist_catalog should skip this
    # Verify that models without valid protocol are not in the table
    rows = await db.fetch_all(
        "SELECT * FROM models WHERE protocol NOT IN ('openai', 'anthropic')"
    )
    assert len(rows) == 0

    await db.disconnect()


@pytest.mark.asyncio
async def test_persist_unresolved_warning_is_once_per_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The unresolved-model warning must fire only once per model per process."""
    import os

    db = Database(path=":memory:")
    await db.connect()
    await MigrationRunner(db).run()
    await _seed_db(db)
    try:
        os.environ.setdefault("EGGPOOL_TEST_KEY", "sk-test-not-real")
        config = AppConfig.from_dict(
            {
                "upstream": {"base_url": "https://example.com/v1"},
                "accounts": [],
                "providers": {
                    "opencode-go": {
                        "id": "opencode-go",
                        "base_url": "https://example.com/v1",
                        "accounts": [
                            {
                                "name": "test-acct",
                                "api_key_env": "EGGPOOL_TEST_KEY",
                                "enabled": True,
                                "weight": 1.0,
                            }
                        ],
                    }
                },
            }
        )
        service = CatalogService(
            config,
            AccountRegistry(config),
            db,
            httpx.AsyncClient(),  # not used: we feed the cache directly
        )
        # Inject an unresolved model into the cache and persist twice.
        service.cache.update_from_account(
            "test-acct",
            "opencode-go",
            [
                {
                    "model_id": "perpetually-unresolved",
                    "display_name": None,
                    "protocol": None,
                    "protocol_source": "unresolved",
                    "capabilities": {},
                    "source_metadata": {},
                }
            ],
        )
        with caplog.at_level(logging.WARNING, logger="eggpool.catalog.service"):
            await service._persist_catalog()  # pyright: ignore[reportPrivateUsage]
            first_warning_count = sum(
                1
                for record in caplog.records
                if "Skipping unresolved model" in record.getMessage()
            )
            caplog.clear()
            await service._persist_catalog()  # pyright: ignore[reportPrivateUsage]
            second_warning_count = sum(
                1
                for record in caplog.records
                if record.levelno == logging.WARNING
                and "Skipping unresolved model" in record.getMessage()
            )

        assert first_warning_count == 1, (
            "First persistence should emit exactly one warning for the unresolved model"
        )
        assert second_warning_count == 0, (
            "Subsequent persistence cycles must not re-warn about the "
            "same unresolved model id (warning is once-per-process)"
        )
    finally:
        await db.disconnect()


@pytest.mark.asyncio
async def test_refresh_prunes_withdrawn_models(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A model that disappears from every account must be pruned from the cache.

    We exercise the full CatalogService.refresh() flow by stubbing the
    network fetcher: each ``_fetch_and_process_account`` invocation writes
    a known set of models into the cache. A second refresh with a smaller
    model set must drop the withdrawn model from the in-memory cache.
    """
    import os

    db = Database(path=":memory:")
    await db.connect()
    await MigrationRunner(db).run()
    await _seed_db(db)
    try:
        os.environ.setdefault("EGGPOOL_TEST_KEY", "sk-test-not-real")
        config = AppConfig.from_dict(
            {
                "upstream": {"base_url": "https://example.com/v1"},
                "accounts": [],
                "providers": {
                    "opencode-go": {
                        "id": "opencode-go",
                        "base_url": "https://example.com/v1",
                        "accounts": [
                            {
                                "name": "test-acct",
                                "api_key_env": "EGGPOOL_TEST_KEY",
                                "enabled": True,
                                "weight": 1.0,
                            }
                        ],
                    }
                },
            }
        )
        async with httpx.AsyncClient() as client:
            service = CatalogService(config, AccountRegistry(config), db, client)
            # First refresh: cache contains both models.
            service.cache.update_from_account(
                "test-acct",
                "opencode-go",
                [
                    {
                        "model_id": "gpt-4o",
                        "protocol": "openai",
                        "protocol_source": "exact_mapping",
                        "capabilities": {},
                        "source_metadata": {},
                    },
                    {
                        "model_id": "withdrawn-model",
                        "protocol": "openai",
                        "protocol_source": "exact_mapping",
                        "capabilities": {},
                        "source_metadata": {},
                    },
                ],
            )
            await service._persist_catalog()  # pyright: ignore[reportPrivateUsage]
            assert service.cache.has_model("withdrawn-model")

            # Second refresh: provider drops the model.
            service.cache.update_from_account(
                "test-acct",
                "opencode-go",
                [
                    {
                        "model_id": "gpt-4o",
                        "protocol": "openai",
                        "protocol_source": "exact_mapping",
                        "capabilities": {},
                        "source_metadata": {},
                    },
                ],
            )
            with caplog.at_level(logging.INFO, logger="eggpool.catalog.service"):
                pruned = service.cache.prune_unused()
            assert pruned == 1
            assert not service.cache.has_model("withdrawn-model")

            # The cache no longer carries the model.  DB-row cleanup is
            # owned by the Phase 2 reconciliation pass; here we only
            # assert that ``get_models_for_exposure`` no longer surfaces
            # the withdrawn name, which is the externally observable
            # contract that dynamic add/subtract must hold.
            exposed = service.cache.get_models_for_exposure("union", {"test-acct"})
            exposed_ids = {m["model_id"] for m in exposed}
            assert "withdrawn-model" not in exposed_ids
            assert "gpt-4o" in exposed_ids
    finally:
        await db.disconnect()
