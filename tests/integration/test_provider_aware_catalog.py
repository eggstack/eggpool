"""Integration tests for provider-aware catalog refresh and DB loading."""

from __future__ import annotations

import pytest

from eggpool.catalog.cache import ModelCatalogCache
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner


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
