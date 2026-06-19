"""Tests for multi-provider support (migration 0015)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import pytest

from go_aggregator.accounts.registry import account_config_rows
from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import MigrationRunner
from go_aggregator.db.repositories import AccountRepository, RequestRepository
from go_aggregator.models.config import AccountConfig, AppConfig, ProviderConfig
from go_aggregator.models.database import AccountRow, ModelRow
from go_aggregator.models.domain import Account, Provider


async def _run_migrations(db: Database) -> None:
    runner = MigrationRunner(db)
    await runner.run()


@pytest.mark.asyncio()
async def test_migration_creates_providers_table() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)

        rows = await database.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='providers'"
        )
        assert len(rows) == 1

        cols = await database.fetch_all("PRAGMA table_info(providers)")
        col_names = {row["name"] for row in cols}
        assert "id" in col_names
        assert "provider_id" in col_names
        assert "base_url" in col_names
        assert "protocols" in col_names
        assert "enabled" in col_names
        assert "created_at" in col_names
    finally:
        await database.disconnect()


@pytest.mark.asyncio()
async def test_migration_adds_provider_id_to_accounts() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)

        async with database.transaction():
            await database.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                "VALUES (?, ?, ?, ?)",
                ("test_acct", "KEY_ENV", 1, 1.0),
            )

        row = await database.fetch_one(
            "SELECT * FROM accounts WHERE name = ?",
            ("test_acct",),
        )
        assert row is not None
        assert row["provider_id"] == "opencode-go"
    finally:
        await database.disconnect()


@pytest.mark.asyncio()
async def test_migration_adds_provider_id_to_models() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)

        async with database.transaction():
            await database.execute_write(
                "INSERT INTO models (model_id, display_name) VALUES (?, ?)",
                ("gpt-4o", "GPT-4o"),
            )

        row = await database.fetch_one(
            "SELECT * FROM models WHERE model_id = ?",
            ("gpt-4o",),
        )
        assert row is not None
        assert row["provider_id"] == "opencode-go"
    finally:
        await database.disconnect()


@pytest.mark.asyncio()
async def test_migration_inserts_default_provider() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)

        row = await database.fetch_one(
            "SELECT * FROM providers WHERE provider_id = ?",
            ("opencode-go",),
        )
        assert row is not None
        assert row["base_url"] == "https://opencode.ai/zen/go/v1"
        assert row["protocols"] == '["openai", "anthropic"]'
        assert row["enabled"] == 1
    finally:
        await database.disconnect()


@pytest.mark.asyncio()
async def test_migration_is_idempotent() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)
        count1 = await database.fetch_one("SELECT COUNT(*) as c FROM _migrations")
        await _run_migrations(database)
        count2 = await database.fetch_one("SELECT COUNT(*) as c FROM _migrations")
        assert count1 is not None
        assert count2 is not None
        assert count1["c"] == count2["c"]
    finally:
        await database.disconnect()


@pytest.mark.asyncio()
async def test_sync_from_config_persists_provider_id() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)
        repo = AccountRepository(database)

        config_accounts: list[dict[str, Any]] = [
            {
                "name": "acct1",
                "api_key_env": "KEY1",
                "enabled": True,
                "weight": 1.0,
                "provider_id": "custom-provider",
            },
            {
                "name": "acct2",
                "api_key_env": "KEY2",
                "enabled": True,
                "weight": 2.0,
            },
        ]

        name_to_id = await repo.sync_from_config(config_accounts)
        assert "acct1" in name_to_id
        assert "acct2" in name_to_id

        row1 = await database.fetch_one(
            "SELECT provider_id FROM accounts WHERE name = ?",
            ("acct1",),
        )
        assert row1 is not None
        assert row1["provider_id"] == "custom-provider"

        row2 = await database.fetch_one(
            "SELECT provider_id FROM accounts WHERE name = ?",
            ("acct2",),
        )
        assert row2 is not None
        assert row2["provider_id"] == "opencode-go"
    finally:
        await database.disconnect()


@pytest.mark.asyncio()
async def test_sync_from_config_updates_provider_id() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)
        repo = AccountRepository(database)

        initial = [{"name": "acct1", "api_key_env": "KEY1"}]
        await repo.sync_from_config(initial)

        updated = [
            {
                "name": "acct1",
                "api_key_env": "KEY1",
                "provider_id": "new-provider",
            }
        ]
        await repo.sync_from_config(updated)

        row = await database.fetch_one(
            "SELECT provider_id FROM accounts WHERE name = ?",
            ("acct1",),
        )
        assert row is not None
        assert row["provider_id"] == "new-provider"
    finally:
        await database.disconnect()


def test_account_config_rows_includes_provider_id() -> None:
    config = AppConfig(
        providers={
            "my-provider": ProviderConfig(
                id="my-provider",
                base_url="https://example.com/v1",
                protocols=["openai"],
                accounts=[
                    AccountConfig(name="a1", api_key_env="K1"),
                    AccountConfig(name="a2", api_key_env="K2"),
                ],
            )
        }
    )
    rows = account_config_rows(config)
    assert len(rows) == 2
    assert rows[0]["name"] == "a1"
    assert rows[0]["provider_id"] == "my-provider"
    assert rows[1]["name"] == "a2"
    assert rows[1]["provider_id"] == "my-provider"


def test_account_config_rows_default_provider() -> None:
    config = AppConfig(
        providers={
            "opencode-go": ProviderConfig(
                id="opencode-go",
                base_url="https://opencode.ai/zen/go/v1",
                protocols=["openai", "anthropic"],
                accounts=[
                    AccountConfig(name="acct", api_key_env="KEY"),
                ],
            )
        }
    )
    rows = account_config_rows(config)
    assert len(rows) == 1
    assert rows[0]["provider_id"] == "opencode-go"


def test_account_config_rows_multiple_providers() -> None:
    config = AppConfig(
        providers={
            "p1": ProviderConfig(
                id="p1",
                base_url="https://p1.example.com/v1",
                protocols=["openai"],
                accounts=[AccountConfig(name="a1", api_key_env="K1")],
            ),
            "p2": ProviderConfig(
                id="p2",
                base_url="https://p2.example.com/v1",
                protocols=["anthropic"],
                accounts=[AccountConfig(name="a2", api_key_env="K2")],
            ),
        }
    )
    rows = account_config_rows(config)
    names = {r["name"] for r in rows}
    assert names == {"a1", "a2"}
    provider_ids = {r["name"]: r["provider_id"] for r in rows}
    assert provider_ids["a1"] == "p1"
    assert provider_ids["a2"] == "p2"


def test_account_config_rows_flat_accounts_normalize() -> None:
    config = AppConfig(
        accounts=[AccountConfig(name="flat_acct", api_key_env="KEY")],
    )
    rows = account_config_rows(config)
    assert len(rows) == 1
    assert rows[0]["name"] == "flat_acct"
    assert rows[0]["provider_id"] == "opencode-go"


def test_database_model_provider_id_defaults() -> None:
    import datetime as _dt

    ns = {"datetime": _dt.datetime}
    AccountRow.model_rebuild(_types_namespace=ns)
    account = AccountRow(
        id=1,
        name="test",
        api_key_env="KEY",
        enabled=True,
        weight=1.0,
        created_at=datetime.now(UTC),
    )
    assert account.provider_id == "opencode-go"

    ModelRow.model_rebuild(_types_namespace=ns)
    model = ModelRow(
        model_id="gpt-4o",
        protocol="openai",
        capabilities="{}",
        source_metadata="{}",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    assert model.provider_id == "opencode-go"


def test_domain_provider_model() -> None:
    import datetime as _dt

    ns = {"datetime": _dt.datetime}
    Provider.model_rebuild(_types_namespace=ns)
    provider = Provider(
        id=1,
        provider_id="test-provider",
        base_url="https://example.com/v1",
        protocols=["openai", "anthropic"],
        enabled=True,
        created_at=datetime.now(UTC),
    )
    assert provider.provider_id == "test-provider"
    assert provider.base_url == "https://example.com/v1"
    assert provider.protocols == ["openai", "anthropic"]
    assert provider.enabled is True


def test_domain_account_model_provider_id() -> None:
    import datetime as _dt

    ns = {"datetime": _dt.datetime}
    Account.model_rebuild(_types_namespace=ns)
    account = Account(
        id=1,
        name="test",
        api_key_env="KEY",
        provider_id="my-provider",
        created_at=datetime.now(UTC),
    )
    assert account.provider_id == "my-provider"


def test_domain_account_model_provider_id_default() -> None:
    import datetime as _dt

    ns = {"datetime": _dt.datetime}
    Account.model_rebuild(_types_namespace=ns)
    account = Account(
        id=1,
        name="test",
        api_key_env="KEY",
        created_at=datetime.now(UTC),
    )
    assert account.provider_id == "opencode-go"


# ---------------------------------------------------------------------------
# AccountRegistry provider-aware tests
# ---------------------------------------------------------------------------


def test_registry_get_provider_for_account() -> None:
    os.environ["REG_P1_KEY"] = "key1"
    os.environ["REG_P2_KEY"] = "key2"
    try:
        config = AppConfig(
            providers={
                "p1": ProviderConfig(
                    id="p1",
                    base_url="https://p1.example.com/v1",
                    protocols=["openai"],
                    accounts=[AccountConfig(name="a1", api_key_env="REG_P1_KEY")],
                ),
                "p2": ProviderConfig(
                    id="p2",
                    base_url="https://p2.example.com/v1",
                    protocols=["anthropic"],
                    accounts=[AccountConfig(name="a2", api_key_env="REG_P2_KEY")],
                ),
            }
        )
        from go_aggregator.accounts.registry import AccountRegistry

        registry = AccountRegistry(config)
        assert registry.get_provider_for_account("a1") == "p1"
        assert registry.get_provider_for_account("a2") == "p2"
        assert registry.get_provider_for_account("nonexistent") is None
    finally:
        del os.environ["REG_P1_KEY"]
        del os.environ["REG_P2_KEY"]


def test_registry_get_accounts_for_provider() -> None:
    os.environ["REG_GP_KEY"] = "key"
    try:
        config = AppConfig(
            providers={
                "p1": ProviderConfig(
                    id="p1",
                    base_url="https://p1.example.com/v1",
                    protocols=["openai"],
                    accounts=[
                        AccountConfig(name="a1", api_key_env="REG_GP_KEY"),
                        AccountConfig(name="a2", api_key_env="REG_GP_KEY"),
                    ],
                ),
                "p2": ProviderConfig(
                    id="p2",
                    base_url="https://p2.example.com/v1",
                    protocols=["anthropic"],
                    accounts=[
                        AccountConfig(name="a3", api_key_env="REG_GP_KEY"),
                    ],
                ),
            }
        )
        from go_aggregator.accounts.registry import AccountRegistry

        registry = AccountRegistry(config)
        p1_accounts = registry.get_accounts_for_provider("p1")
        assert len(p1_accounts) == 2
        names = {s.name for s in p1_accounts}
        assert names == {"a1", "a2"}

        p2_accounts = registry.get_accounts_for_provider("p2")
        assert len(p2_accounts) == 1
        assert p2_accounts[0].name == "a3"

        empty = registry.get_accounts_for_provider("nonexistent")
        assert empty == []
    finally:
        del os.environ["REG_GP_KEY"]


def test_registry_get_enabled_accounts_for_provider() -> None:
    os.environ["REG_GE_KEY"] = "key"
    try:
        config = AppConfig(
            providers={
                "p1": ProviderConfig(
                    id="p1",
                    base_url="https://p1.example.com/v1",
                    protocols=["openai"],
                    accounts=[
                        AccountConfig(name="a1", api_key_env="REG_GE_KEY"),
                        AccountConfig(
                            name="a2",
                            api_key_env="REG_GE_KEY",
                            enabled=False,
                        ),
                    ],
                ),
            }
        )
        from go_aggregator.accounts.registry import AccountRegistry

        registry = AccountRegistry(config)
        enabled = registry.get_enabled_accounts_for_provider("p1")
        assert len(enabled) == 1
        assert enabled[0].name == "a1"
        assert enabled[0].enabled is True
    finally:
        del os.environ["REG_GE_KEY"]


def test_registry_get_provider_ids() -> None:
    os.environ["REG_PID_KEY"] = "key"
    try:
        config = AppConfig(
            providers={
                "p1": ProviderConfig(
                    id="p1",
                    base_url="https://p1.example.com/v1",
                    protocols=["openai"],
                    accounts=[AccountConfig(name="a1", api_key_env="REG_PID_KEY")],
                ),
                "p2": ProviderConfig(
                    id="p2",
                    base_url="https://p2.example.com/v1",
                    protocols=["anthropic"],
                    accounts=[
                        AccountConfig(name="a2", api_key_env="REG_PID_KEY"),
                        AccountConfig(name="a3", api_key_env="REG_PID_KEY"),
                    ],
                ),
            }
        )
        from go_aggregator.accounts.registry import AccountRegistry

        registry = AccountRegistry(config)
        ids = registry.get_provider_ids()
        assert set(ids) == {"p1", "p2"}
    finally:
        del os.environ["REG_PID_KEY"]


def test_registry_reports_provider_protocols() -> None:
    os.environ["REG_PROTO_KEY"] = "key"
    try:
        config = AppConfig(
            providers={
                "p1": ProviderConfig(
                    id="p1",
                    base_url="https://p1.example.com/v1",
                    protocols=["openai"],
                    accounts=[AccountConfig(name="a1", api_key_env="REG_PROTO_KEY")],
                )
            }
        )
        from go_aggregator.accounts.registry import AccountRegistry

        registry = AccountRegistry(config)

        assert registry.get_provider_protocols("p1") == {"openai"}
        assert registry.get_provider_protocols("missing") == set()
        assert registry.account_supports_protocol("a1", "openai") is True
        assert registry.account_supports_protocol("a1", "anthropic") is False
        assert registry.account_supports_protocol("missing", "openai") is False
    finally:
        del os.environ["REG_PROTO_KEY"]


@pytest.mark.asyncio()
async def test_create_pending_persists_provider_id() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)
        repo = RequestRepository(database)

        async with database.transaction():
            await database.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                "VALUES (?, ?, ?, ?)",
                ("acct1", "KEY1", 1, 1.0),
            )
            await database.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("gpt-4", "openai"),
            )
            request_id = await repo.create_pending(
                request_id="test-req-1",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
                provider_id="custom-provider",
            )
        assert request_id

        row = await database.fetch_one(
            "SELECT provider_id FROM requests WHERE id = ?",
            (request_id,),
        )
        assert row is not None
        assert row["provider_id"] == "custom-provider"
    finally:
        await database.disconnect()


@pytest.mark.asyncio()
async def test_create_pending_defaults_provider_id() -> None:
    database = Database(path=":memory:")
    await database.connect()
    try:
        await _run_migrations(database)
        repo = RequestRepository(database)

        async with database.transaction():
            await database.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                "VALUES (?, ?, ?, ?)",
                ("acct1", "KEY1", 1, 1.0),
            )
            await database.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("gpt-4", "openai"),
            )
            request_id = await repo.create_pending(
                request_id="test-req-2",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
        assert request_id

        row = await database.fetch_one(
            "SELECT provider_id FROM requests WHERE id = ?",
            (request_id,),
        )
        assert row is not None
        assert row["provider_id"] == "opencode-go"
    finally:
        await database.disconnect()


def test_registry_multiple_providers_default_normalize() -> None:
    os.environ["REG_DEF_KEY"] = "key"
    try:
        config = AppConfig(
            accounts=[
                AccountConfig(name="flat", api_key_env="REG_DEF_KEY"),
            ]
        )
        from go_aggregator.accounts.registry import AccountRegistry

        registry = AccountRegistry(config)
        assert registry.get_provider_for_account("flat") == "opencode-go"
        assert registry.get_provider_ids() == ["opencode-go"]
    finally:
        del os.environ["REG_DEF_KEY"]
