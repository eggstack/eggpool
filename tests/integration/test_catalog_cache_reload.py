"""Integration tests for cache reload protocol source consistency (Phase 14)."""

from __future__ import annotations

import httpx
import pytest

from eggpool.accounts.registry import AccountRegistry
from eggpool.catalog.cache import ModelCatalogCache
from eggpool.catalog.service import CatalogService
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.models.config import AppConfig


def test_load_model_preserves_protocol_source() -> None:
    """Loading a model from DB preserves protocol_source."""
    cache = ModelCatalogCache()

    cache.load_model(
        model_id="gpt-4o",
        display_name="GPT-4o",
        protocol="openai",
        capabilities={},
        source_metadata={},
        protocol_source="exact_mapping",
    )

    model = cache.get_model("gpt-4o")
    assert model is not None
    assert model["protocol"] == "openai"
    assert model["protocol_source"] == "exact_mapping"


def test_load_model_default_protocol_source() -> None:
    """Loading a model without protocol_source defaults to None."""
    cache = ModelCatalogCache()

    cache.load_model(
        model_id="gpt-4o",
        display_name="GPT-4o",
        protocol="openai",
        capabilities={},
        source_metadata={},
    )

    model = cache.get_model("gpt-4o")
    assert model is not None
    assert model.get("protocol_source") is None


def test_update_from_account_preserves_source() -> None:
    """Updating from account preserves protocol_source in cache."""
    cache = ModelCatalogCache()

    cache.update_from_account(
        "acct-a",
        "opencode-go",
        [
            {
                "model_id": "claude-3",
                "display_name": "Claude 3",
                "protocol": "anthropic",
                "protocol_source": "family_mapping",
                "capabilities": {},
                "source_metadata": {},
            },
        ],
    )

    model = cache.get_model("claude-3")
    assert model is not None
    assert model["protocol_source"] == "family_mapping"


def test_refresh_fallback_preserves_persisted_source() -> None:
    """When refresh has no resolution hints, persisted protocol is used."""
    cache = ModelCatalogCache()

    # Simulate a previously loaded model from DB
    cache.load_model(
        model_id="custom-model",
        display_name="Custom",
        protocol="openai",
        capabilities={},
        source_metadata={},
        protocol_source="persisted",
    )

    # Simulate a refresh that provides no resolution metadata
    cache.update_from_account(
        "acct-a",
        "opencode-go",
        [
            {
                "model_id": "custom-model",
                "display_name": "Custom",
                "protocol": "openai",
                "protocol_source": "persisted",
                "capabilities": {},
                "source_metadata": {},
            },
        ],
    )

    model = cache.get_model("custom-model")
    assert model is not None
    assert model["protocol"] == "openai"
    assert model["protocol_source"] == "persisted"


@pytest.mark.asyncio
async def test_corrupt_metadata_does_not_abort_cache_hydration() -> None:
    """One bad advisory metadata field must not hide the durable catalog."""
    db = Database(path=":memory:")
    await db.connect()
    await MigrationRunner(db).run()
    try:
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts "
                "(name, api_key_env, enabled, weight, provider_id) "
                "VALUES (?, ?, 1, 1.0, ?)",
                ("acct-a", "KEY_A", "opencode-go"),
            )
            await db.execute_write(
                "INSERT INTO models "
                "(model_id, protocol, resolution_status, capabilities, "
                "source_metadata) VALUES (?, ?, ?, ?, ?)",
                ("broken-meta", "openai", "resolved", "{invalid", "[]"),
            )
            await db.execute_write(
                "INSERT INTO models "
                "(model_id, protocol, resolution_status, capabilities, "
                "source_metadata) VALUES (?, ?, ?, ?, ?)",
                ("valid-model", "openai", "resolved", '{"vision": true}', "{}"),
            )
            account = await db.fetch_one(
                "SELECT id FROM accounts WHERE name = ?", ("acct-a",)
            )
            assert account is not None
            for model_id in ("broken-meta", "valid-model"):
                await db.execute_write(
                    "INSERT INTO account_models (account_id, model_id, enabled) "
                    "VALUES (?, ?, 1)",
                    (account["id"], model_id),
                )

        config = AppConfig.from_dict({"accounts": []})
        async with httpx.AsyncClient() as client:
            service = CatalogService(
                config,
                AccountRegistry(config),
                db,
                client,
            )
            await service._load_cached_models()  # pyright: ignore[reportPrivateUsage]

        broken = service.cache.get_model("broken-meta")
        valid = service.cache.get_model("valid-model")
        assert broken is not None
        assert broken["capabilities"] == {}
        assert broken["source_metadata"] == {}
        assert valid is not None
        assert valid["capabilities"] == {"vision": True}
        assert service.cache.get_supporting_accounts("broken-meta") == {"acct-a"}
        assert service.cache.get_supporting_accounts("valid-model") == {"acct-a"}
    finally:
        await db.disconnect()
