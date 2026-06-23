"""Integration tests for provider-aware catalog refresh and DB loading."""

from __future__ import annotations

import httpx
import pytest

from eggpool.accounts.registry import AccountRegistry
from eggpool.catalog.cache import ModelCatalogCache
from eggpool.catalog.service import CatalogService
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.models.config import AccountConfig, AppConfig, ProviderConfig


@pytest.mark.asyncio
async def test_load_cached_models_sets_provider_id() -> None:
    """_load_cached_models loads provider_id from accounts table."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()

    # Insert accounts with different providers
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight, provider_id) "
            "VALUES (?, ?, 1, 1.0, ?)",
            ("acct-alpha", "KEY_A", "provider-a"),
        )
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight, provider_id) "
            "VALUES (?, ?, 1, 1.0, ?)",
            ("acct-beta", "KEY_B", "provider-b"),
        )

    # Insert models
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO models (model_id, protocol, resolution_status) "
            "VALUES (?, ?, ?)",
            ("gpt-4", "openai", "resolved"),
        )
        await db.execute_write(
            "INSERT INTO models (model_id, protocol, resolution_status) "
            "VALUES (?, ?, ?)",
            ("claude-3", "anthropic", "resolved"),
        )

    # Insert account-model relationships
    async with db.transaction():
        acct_a = await db.fetch_one(
            "SELECT id FROM accounts WHERE name = ?", ("acct-alpha",)
        )
        acct_b = await db.fetch_one(
            "SELECT id FROM accounts WHERE name = ?", ("acct-beta",)
        )
        assert acct_a is not None
        assert acct_b is not None
        await db.execute_write(
            "INSERT INTO account_models (account_id, model_id, enabled) "
            "VALUES (?, ?, 1)",
            (acct_a["id"], "gpt-4"),
        )
        await db.execute_write(
            "INSERT INTO account_models (account_id, model_id, enabled) "
            "VALUES (?, ?, 1)",
            (acct_b["id"], "claude-3"),
        )

    # Create a cache and manually run the same load logic as _load_cached_models
    cache = ModelCatalogCache()

    # Load models
    rows = await db.fetch_all("SELECT * FROM models")
    for row in rows:
        display_name = row["display_name"] if row["display_name"] else None
        cache.load_model(
            model_id=row["model_id"],
            display_name=display_name,
            protocol=row["protocol"],
            capabilities={},
            source_metadata={},
        )

    # Load account-model relationships with provider info
    am_rows = await db.fetch_all(
        "SELECT account_id, model_id FROM account_models WHERE enabled = 1"
    )
    acct_rows = await db.fetch_all("SELECT id, name, provider_id FROM accounts")
    id_to_name = {row["id"]: row["name"] for row in acct_rows}
    id_to_provider: dict[int, str] = {
        row["id"]: row["provider_id"] for row in acct_rows
    }

    for row in am_rows:
        account_name = id_to_name.get(row["account_id"])
        provider_id = id_to_provider.get(row["account_id"], "opencode-go")
        if account_name:
            cache.set_account_provider(account_name, provider_id)
            if cache.has_model(row["model_id"]):
                cache.add_account_support(row["model_id"], account_name)

    # Verify provider tracking
    assert cache.get_provider_for_account("acct-alpha") == "provider-a"
    assert cache.get_provider_for_account("acct-beta") == "provider-b"

    # Verify provider-suffixed exposure
    result = cache.get_provider_suffixed_models("union", {"acct-alpha", "acct-beta"})
    ids = {m["model_id"] for m in result}
    assert ids == {"claude-3/provider-b", "gpt-4/provider-a"}

    await db.disconnect()


@pytest.mark.asyncio
async def test_persist_catalog_stores_provider_id() -> None:
    """Accounts table stores provider_id correctly."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()

    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight, provider_id) "
            "VALUES (?, ?, 1, 1.0, ?)",
            ("my-acct", "MY_KEY", "custom-provider"),
        )

    rows = await db.fetch_all("SELECT name, provider_id FROM accounts")
    assert len(rows) == 1
    assert rows[0]["name"] == "my-acct"
    assert rows[0]["provider_id"] == "custom-provider"

    await db.disconnect()


@pytest.mark.asyncio
async def test_shared_model_provider_metadata_survives_cached_restart() -> None:
    """Shared IDs retain independent protocols and metadata after persistence."""
    db = Database(path=":memory:")
    await db.connect()
    await MigrationRunner(db).run()
    config = AppConfig(
        providers={
            "provider-a": ProviderConfig(
                id="provider-a",
                base_url="https://provider-a.example",
                protocols=["openai"],
                accounts=[AccountConfig(name="acct-a", api_key="key-a")],
            ),
            "provider-b": ProviderConfig(
                id="provider-b",
                base_url="https://provider-b.example",
                protocols=["anthropic"],
                accounts=[AccountConfig(name="acct-b", api_key="key-b")],
            ),
        }
    )
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, provider_id) VALUES (?, ?, ?)",
            ("acct-a", "", "provider-a"),
        )
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, provider_id) VALUES (?, ?, ?)",
            ("acct-b", "", "provider-b"),
        )

    client = httpx.AsyncClient()
    try:
        service = CatalogService(config, AccountRegistry(config), db, client)
        service.cache.update_from_account(
            "acct-a",
            "provider-a",
            [
                {
                    "model_id": "shared-model",
                    "protocol": "openai",
                    "capabilities": {"context_length": 100_000},
                }
            ],
        )
        service.cache.update_from_account(
            "acct-b",
            "provider-b",
            [
                {
                    "model_id": "shared-model",
                    "protocol": "anthropic",
                    "capabilities": {"context_length": 200_000},
                }
            ],
        )
        await service._persist_catalog()  # pyright: ignore[reportPrivateUsage]

        restarted = CatalogService(config, AccountRegistry(config), db, client)
        await restarted._load_cached_models()  # pyright: ignore[reportPrivateUsage]

        provider_a = restarted.cache.get_provider_model_entry(
            "shared-model", "provider-a"
        )
        provider_b = restarted.cache.get_provider_model_entry(
            "shared-model", "provider-b"
        )
        assert provider_a is not None
        assert provider_b is not None
        assert provider_a["protocol"] == "openai"
        assert provider_b["protocol"] == "anthropic"
        assert provider_a["capabilities"]["context_length"] == 100_000
        assert provider_b["capabilities"]["context_length"] == 200_000
    finally:
        await client.aclose()
        await db.disconnect()


@pytest.mark.asyncio
async def test_catalog_persists_only_provider_specific_pricing() -> None:
    """Catalog refreshes neither create phantom nor duplicate price rows."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    config = AppConfig(
        providers={
            "provider-a": ProviderConfig(
                id="provider-a",
                base_url="https://provider-a.example",
            )
        }
    )
    registry = AccountRegistry(config)
    client = httpx.AsyncClient()
    try:
        service = CatalogService(config, registry, db, client)
        service.cache.update_from_account(
            "account-a",
            "provider-a",
            [
                {
                    "model_id": "shared-model",
                    "protocol": "openai",
                    "source_metadata": {
                        "input_price_per_1k": 0.001,
                        "output_price_per_1k": 0.002,
                    },
                }
            ],
        )

        await service._persist_catalog()  # pyright: ignore[reportPrivateUsage]
        await service._persist_catalog()  # pyright: ignore[reportPrivateUsage]

        rows = await db.fetch_all(
            "SELECT provider_id FROM model_price_snapshots WHERE model_id = ?",
            ("shared-model",),
        )
        assert [row["provider_id"] for row in rows] == ["provider-a"]
    finally:
        await client.aclose()
        await db.disconnect()


@pytest.mark.asyncio
async def test_catalog_persistence_preserves_observation_timestamps() -> None:
    """Persisting the catalog must not make unrefreshed entries look fresh."""
    db = Database(path=":memory:")
    await db.connect()
    await MigrationRunner(db).run()
    config = AppConfig.from_dict({"accounts": []})
    client = httpx.AsyncClient()
    try:
        service = CatalogService(config, AccountRegistry(config), db, client)
        first_seen = 1_577_836_800.0  # 2020-01-01 UTC
        last_seen = 1_609_459_200.0  # 2021-01-01 UTC
        service.cache.load_model(
            "cached-model",
            None,
            "openai",
            {},
            {},
            first_seen_at=first_seen,
            last_seen_at=last_seen,
        )
        service.cache.set_provider_model_entry(
            "cached-model",
            "provider-a",
            {
                "model_id": "cached-model",
                "protocol": "openai",
                "capabilities": {},
                "source_metadata": {},
                "first_seen_at": first_seen,
                "last_seen_at": last_seen,
            },
        )

        await service._persist_catalog()  # pyright: ignore[reportPrivateUsage]

        model = await db.fetch_one(
            "SELECT first_seen_at, last_seen_at FROM models WHERE model_id = ?",
            ("cached-model",),
        )
        provider_model = await db.fetch_one(
            "SELECT first_seen_at, last_seen_at FROM provider_model_metadata "
            "WHERE model_id = ? AND provider_id = ?",
            ("cached-model", "provider-a"),
        )
        assert model is not None
        assert provider_model is not None
        assert model["first_seen_at"] == "2020-01-01 00:00:00"
        assert model["last_seen_at"] == "2021-01-01 00:00:00"
        assert provider_model["first_seen_at"] == "2020-01-01 00:00:00"
        assert provider_model["last_seen_at"] == "2021-01-01 00:00:00"
    finally:
        await client.aclose()
        await db.disconnect()
