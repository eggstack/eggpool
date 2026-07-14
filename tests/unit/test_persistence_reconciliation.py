"""AC#11: Account/provider reconciliation stable IDs.

Tests that ProviderRepository and AccountRepository sync_from_config
behaves correctly for idempotent no-ops, credential-only updates,
and disable/enable cycles.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner


async def _run_migrations(db: Database) -> None:
    runner = MigrationRunner(db)
    await runner.run()


@pytest_asyncio.fixture
async def db() -> Database:
    database = Database(path=":memory:")
    await database.connect()
    await _run_migrations(database)
    yield database
    await database.disconnect()


class TestProviderReconciliation:
    """ProviderRepository.sync_from_config preserves provider identity."""

    @pytest.mark.asyncio
    async def test_repeated_noop_creates_no_extra_rows(self, db: Database) -> None:
        """Syncing the same config twice produces exactly one row."""
        from eggpool.db.repositories import ProviderRepository

        repo = ProviderRepository(db)
        config = {
            "opencode-go": {
                "base_url": "https://opencode.ai/zen/go/v1",
                "protocols": ["openai"],
            }
        }
        await repo.sync_from_config(config)
        await repo.sync_from_config(config)

        rows = await db.fetch_all("SELECT * FROM providers")
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["provider_id"] == "opencode-go"
        assert row["enabled"] == 1

    @pytest.mark.asyncio
    async def test_disable_enable_cycle_preserves_row(self, db: Database) -> None:
        """Disabling then re-enabling a provider keeps the same row."""
        from eggpool.db.repositories import ProviderRepository

        repo = ProviderRepository(db)
        config = {
            "opencode-go": {
                "base_url": "https://opencode.ai/zen/go/v1",
                "protocols": ["openai"],
            }
        }
        await repo.sync_from_config(config)
        rows_before = await db.fetch_all(
            "SELECT id, provider_id FROM providers WHERE provider_id = ?",
            ("opencode-go",),
        )
        original_id = rows_before[0]["id"]

        # Disable by syncing with empty config
        await repo.sync_from_config({})
        disabled = await db.fetch_one(
            "SELECT enabled FROM providers WHERE provider_id = ?",
            ("opencode-go",),
        )
        assert disabled is not None
        assert disabled["enabled"] == 0

        # Re-enable by syncing original config
        await repo.sync_from_config(config)
        rows_after = await db.fetch_all(
            "SELECT id, provider_id FROM providers WHERE provider_id = ?",
            ("opencode-go",),
        )
        assert len(rows_after) == 1
        assert rows_after[0]["id"] == original_id

    @pytest.mark.asyncio
    async def test_upsert_updates_fields_preserving_id(self, db: Database) -> None:
        """Changing base_url upserts without creating a new row."""
        from eggpool.db.repositories import ProviderRepository

        repo = ProviderRepository(db)
        await repo.sync_from_config(
            {
                "opencode-go": {
                    "base_url": "https://old.example.com/v1",
                    "protocols": ["openai"],
                }
            }
        )
        rows_before = await db.fetch_all(
            "SELECT id FROM providers WHERE provider_id = ?",
            ("opencode-go",),
        )
        original_id = rows_before[0]["id"]

        await repo.sync_from_config(
            {
                "opencode-go": {
                    "base_url": "https://new.example.com/v1",
                    "protocols": ["openai"],
                }
            }
        )
        rows_after = await db.fetch_all(
            "SELECT id, base_url FROM providers WHERE provider_id = ?",
            ("opencode-go",),
        )
        assert len(rows_after) == 1
        assert rows_after[0]["id"] == original_id
        assert rows_after[0]["base_url"] == "https://new.example.com/v1"


class TestAccountReconciliation:
    """AccountRepository.sync_from_config preserves account identity."""

    @pytest.mark.asyncio
    async def test_repeated_noop_creates_no_extra_rows(self, db: Database) -> None:
        """Syncing the same config twice produces exactly one row with same ID."""
        from eggpool.db.repositories import AccountRepository

        repo = AccountRepository(db)
        accounts = [
            {
                "name": "default",
                "api_key_env": "SERVER_API_KEY",
                "enabled": True,
                "weight": 1.0,
                "provider_id": "opencode-go",
            }
        ]
        ids1 = await repo.sync_from_config(accounts)
        ids2 = await repo.sync_from_config(accounts)

        assert ids1["default"] == ids2["default"]
        rows = await db.fetch_all("SELECT * FROM accounts WHERE name = ?", ("default",))
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_credential_only_update_preserves_account_id(
        self, db: Database
    ) -> None:
        """Changing api_key_env does not change the account ID."""
        from eggpool.db.repositories import AccountRepository

        repo = AccountRepository(db)
        accounts_v1 = [
            {
                "name": "default",
                "api_key_env": "OLD_API_KEY",
                "enabled": True,
                "weight": 1.0,
                "provider_id": "opencode-go",
            }
        ]
        ids_v1 = await repo.sync_from_config(accounts_v1)
        original_id = ids_v1["default"]

        accounts_v2 = [
            {
                "name": "default",
                "api_key_env": "NEW_API_KEY",
                "enabled": True,
                "weight": 1.0,
                "provider_id": "opencode-go",
            }
        ]
        ids_v2 = await repo.sync_from_config(accounts_v2)
        assert ids_v2["default"] == original_id

        row = await db.fetch_one(
            "SELECT api_key_env FROM accounts WHERE name = ?", ("default",)
        )
        assert row is not None
        assert row["api_key_env"] == "NEW_API_KEY"

    @pytest.mark.asyncio
    async def test_disable_enable_cycle_preserves_history(self, db: Database) -> None:
        """Disabling then re-enabling preserves the original row ID."""
        from eggpool.db.repositories import AccountRepository

        repo = AccountRepository(db)
        accounts = [
            {
                "name": "default",
                "api_key_env": "SERVER_API_KEY",
                "enabled": True,
                "weight": 1.0,
                "provider_id": "opencode-go",
            }
        ]
        ids_before = await repo.sync_from_config(accounts)
        original_id = ids_before["default"]

        # Disable by syncing with empty list
        await repo.sync_from_config([])
        disabled = await db.fetch_one(
            "SELECT enabled FROM accounts WHERE name = ?", ("default",)
        )
        assert disabled is not None
        assert disabled["enabled"] == 0

        # Re-enable by syncing original config
        ids_after = await repo.sync_from_config(accounts)
        assert ids_after["default"] == original_id
        enabled = await db.fetch_one(
            "SELECT enabled FROM accounts WHERE name = ?", ("default",)
        )
        assert enabled is not None
        assert enabled["enabled"] == 1

    @pytest.mark.asyncio
    async def test_weight_change_preserves_account_id(self, db: Database) -> None:
        """Changing weight upserts without creating a new row."""
        from eggpool.db.repositories import AccountRepository

        repo = AccountRepository(db)
        await repo.sync_from_config(
            [
                {
                    "name": "default",
                    "api_key_env": "SERVER_API_KEY",
                    "enabled": True,
                    "weight": 1.0,
                    "provider_id": "opencode-go",
                }
            ]
        )
        rows_before = await db.fetch_all(
            "SELECT id FROM accounts WHERE name = ?", ("default",)
        )
        original_id = rows_before[0]["id"]

        await repo.sync_from_config(
            [
                {
                    "name": "default",
                    "api_key_env": "SERVER_API_KEY",
                    "enabled": True,
                    "weight": 2.0,
                    "provider_id": "opencode-go",
                }
            ]
        )
        rows_after = await db.fetch_all(
            "SELECT id, weight FROM accounts WHERE name = ?", ("default",)
        )
        assert len(rows_after) == 1
        assert rows_after[0]["id"] == original_id
        assert rows_after[0]["weight"] == 2.0

    @pytest.mark.asyncio
    async def test_multiple_accounts_preserve_independent_ids(
        self, db: Database
    ) -> None:
        """Multiple accounts get distinct IDs that survive updates."""
        from eggpool.db.repositories import AccountRepository

        repo = AccountRepository(db)
        accounts = [
            {
                "name": "acct-a",
                "api_key_env": "KEY_A",
                "enabled": True,
                "weight": 1.0,
                "provider_id": "opencode-go",
            },
            {
                "name": "acct-b",
                "api_key_env": "KEY_B",
                "enabled": True,
                "weight": 1.0,
                "provider_id": "opencode-go",
            },
        ]
        ids1 = await repo.sync_from_config(accounts)
        assert ids1["acct-a"] != ids1["acct-b"]

        ids2 = await repo.sync_from_config(accounts)
        assert ids2["acct-a"] == ids1["acct-a"]
        assert ids2["acct-b"] == ids1["acct-b"]
