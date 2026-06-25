"""Integration tests for the catalog reconciliation pass.

The reconciliation pass runs as part of ``CatalogService._persist_catalog``
after the live cache has been upserted.  It is responsible for keeping
the durable ``models`` and ``provider_model_metadata`` tables aligned
with the live in-memory cache so the database does not accumulate
orphans when a provider withdraws a model.

When a durable model row is no longer present in the live cache, the
pass:

* relinks any historical ``requests`` and ``reservations`` rows to
  ``__deprecated__`` while preserving the original id in
  ``original_model_id``;
* deletes the now-orphan ``models`` row, its
  ``provider_model_metadata`` rows, and any
  ``account_models`` link rows;
* leaves models with surviving request activity attributable under
  the real name in the dashboard (queries fall back to
  ``original_model_id``).
"""

from __future__ import annotations

import os

import httpx
import pytest

from eggpool.accounts.registry import AccountRegistry
from eggpool.catalog.service import CatalogService
from eggpool.constants import DEPRECATED_MODEL_ID
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import (
    RequestRepository,
    ReservationRepository,
)
from eggpool.models.config import AppConfig


def _config() -> AppConfig:
    os.environ.setdefault("EGGPOOL_TEST_KEY", "sk-test-not-real")
    return AppConfig.from_dict(
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


async def _seed_request(
    db: Database,
    account_name: str,
    model_id: str,
) -> int:
    """Insert a finished request row for ``(account, model)``."""
    async with db.transaction():
        account = await db.fetch_one(
            "SELECT id FROM accounts WHERE name = ?", (account_name,)
        )
        if account is None:
            await db.execute_write(
                "INSERT INTO accounts "
                "(name, api_key_env, enabled, weight, provider_id) "
                "VALUES (?, ?, 1, 1.0, ?)",
                (account_name, "EGGPOOL_TEST_KEY", "opencode-go"),
            )
            account = await db.fetch_one(
                "SELECT id FROM accounts WHERE name = ?", (account_name,)
            )
        assert account is not None
        account_id = int(account["id"])
        request_repo = RequestRepository(db)
        request_id = await request_repo.create_pending(
            request_id=f"r-{model_id}",
            model_id=model_id,
            protocol="openai",
            streamed=False,
            account_id=account_id,
            reserved_microdollars=0,
        )
        await request_repo.update_after_completion(
            request_id,
            status="completed",
            input_tokens=10,
            output_tokens=20,
            cost_microdollars=300,
        )
        return int(request_id)


async def _seed_reservation(
    db: Database,
    account_name: str,
    model_id: str,
    request_id: int,
) -> None:
    async with db.transaction():
        account = await db.fetch_one(
            "SELECT id FROM accounts WHERE name = ?", (account_name,)
        )
        if account is None:
            await db.execute_write(
                "INSERT INTO accounts "
                "(name, api_key_env, enabled, weight, provider_id) "
                "VALUES (?, ?, 1, 1.0, ?)",
                (account_name, "EGGPOOL_TEST_KEY", "opencode-go"),
            )
            account = await db.fetch_one(
                "SELECT id FROM accounts WHERE name = ?", (account_name,)
            )
        assert account is not None
        reservation_repo = ReservationRepository(db)
        await reservation_repo.create(
            request_id=str(request_id),
            account_id=int(account["id"]),
            model_id=model_id,
            estimated_tokens=10,
            estimated_microdollars=300,
        )


async def _seed_model(db: Database, model_id: str, account_name: str) -> None:
    async with db.transaction():
        account = await db.fetch_one(
            "SELECT id FROM accounts WHERE name = ?", (account_name,)
        )
        if account is None:
            await db.execute_write(
                "INSERT INTO accounts "
                "(name, api_key_env, enabled, weight, provider_id) "
                "VALUES (?, ?, 1, 1.0, ?)",
                (account_name, "EGGPOOL_TEST_KEY", "opencode-go"),
            )
            account = await db.fetch_one(
                "SELECT id FROM accounts WHERE name = ?", (account_name,)
            )
        assert account is not None
        account_id = int(account["id"])
        await db.execute_write(
            "INSERT OR IGNORE INTO models "
            "(model_id, protocol, resolution_status, provider_id) "
            "VALUES (?, ?, ?, ?)",
            (model_id, "openai", "resolved", "opencode-go"),
        )
        await db.execute_write(
            "INSERT OR REPLACE INTO account_models "
            "(account_id, model_id, enabled) VALUES (?, ?, 1)",
            (account_id, model_id),
        )
        await db.execute_write(
            "INSERT OR REPLACE INTO provider_model_metadata "
            "(model_id, provider_id, protocol, resolution_status) "
            "VALUES (?, ?, ?, ?)",
            (model_id, "opencode-go", "openai", "resolved"),
        )


@pytest.mark.asyncio
async def test_reconcile_deletes_unreferenced_model() -> None:
    """A model with no historical usage is deleted on the next persist."""
    db = Database(path=":memory:")
    await db.connect()
    await MigrationRunner(db).run()
    try:
        config = _config()
        async with httpx.AsyncClient() as client:
            service = CatalogService(config, AccountRegistry(config), db, client)
            # Seed a model and provider row that the live cache no longer carries.
            await _seed_model(db, "withdrawn-fresh", "test-acct")
            rows = await db.fetch_all(
                "SELECT model_id FROM models WHERE model_id = ?",
                ("withdrawn-fresh",),
            )
            assert len(rows) == 1

            # Live cache is empty, so the reconciliation pass should
            # drop the model row and its provider/account links.
            await service._persist_catalog()  # pyright: ignore[reportPrivateUsage]

            rows = await db.fetch_all(
                "SELECT model_id FROM models WHERE model_id = ?",
                ("withdrawn-fresh",),
            )
            assert rows == []
            links = await db.fetch_all(
                "SELECT model_id FROM account_models WHERE model_id = ?",
                ("withdrawn-fresh",),
            )
            assert links == []
            provider = await db.fetch_all(
                "SELECT model_id FROM provider_model_metadata WHERE model_id = ?",
                ("withdrawn-fresh",),
            )
            assert provider == []
            placeholder = await db.fetch_all(
                "SELECT model_id FROM models WHERE model_id = ?",
                (DEPRECATED_MODEL_ID,),
            )
            assert len(placeholder) == 1
    finally:
        await db.disconnect()


@pytest.mark.asyncio
async def test_reconcile_relinks_model_with_history() -> None:
    """A withdrawn model with usage data is relinked to the placeholder."""
    db = Database(path=":memory:")
    await db.connect()
    await MigrationRunner(db).run()
    try:
        config = _config()
        await _seed_model(db, "withdrawn-historic", "test-acct")
        request_id = await _seed_request(db, "test-acct", "withdrawn-historic")
        await _seed_reservation(db, "test-acct", "withdrawn-historic", request_id)

        async with httpx.AsyncClient() as client:
            service = CatalogService(config, AccountRegistry(config), db, client)
            await service._persist_catalog()  # pyright: ignore[reportPrivateUsage]

        # Original model row is gone.
        original = await db.fetch_all(
            "SELECT model_id FROM models WHERE model_id = ?",
            ("withdrawn-historic",),
        )
        assert original == []

        # Requests and reservations are now under the placeholder and
        # remember the original id.
        relinked = await db.fetch_all(
            "SELECT model_id, original_model_id FROM requests "
            "WHERE original_model_id = ?",
            ("withdrawn-historic",),
        )
        assert len(relinked) == 1
        assert relinked[0]["model_id"] == DEPRECATED_MODEL_ID
        assert relinked[0]["original_model_id"] == "withdrawn-historic"

        resv = await db.fetch_all(
            "SELECT model_id, original_model_id FROM reservations "
            "WHERE original_model_id = ?",
            ("withdrawn-historic",),
        )
        assert len(resv) == 1
        assert resv[0]["model_id"] == DEPRECATED_MODEL_ID

        # Stats queries still surface the original name.
        from eggpool.stats import queries

        rows = await queries.fetch_model_stats(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        ids = {r["model_id"] for r in rows}
        assert "withdrawn-historic" in ids
        assert DEPRECATED_MODEL_ID not in ids

        # The timeseries filter still works for the original id.
        series = await queries.fetch_timeseries(
            db,
            "2000-01-01 00:00:00",
            "2099-12-31 23:59:59",
            model_id="withdrawn-historic",
        )
        assert any(r["request_count"] >= 1 for r in series)
    finally:
        await db.disconnect()


@pytest.mark.asyncio
async def test_reconcile_removes_orphan_provider_rows() -> None:
    """Provider rows for withdrawn models are cleaned up."""
    db = Database(path=":memory:")
    await db.connect()
    await MigrationRunner(db).run()
    try:
        config = _config()
        await _seed_model(db, "withdrawn-orphan", "test-acct")
        async with httpx.AsyncClient() as client:
            service = CatalogService(config, AccountRegistry(config), db, client)
            await service._persist_catalog()  # pyright: ignore[reportPrivateUsage]
        rows = await db.fetch_all(
            "SELECT model_id FROM provider_model_metadata WHERE model_id = ?",
            ("withdrawn-orphan",),
        )
        assert rows == []
    finally:
        await db.disconnect()


@pytest.mark.asyncio
async def test_reconcile_preserves_live_models() -> None:
    """Models still in the live cache are not touched by reconciliation."""
    db = Database(path=":memory:")
    await db.connect()
    await MigrationRunner(db).run()
    try:
        config = _config()
        await _seed_model(db, "still-here", "test-acct")
        async with httpx.AsyncClient() as client:
            service = CatalogService(config, AccountRegistry(config), db, client)
            service.cache.update_from_account(
                "test-acct",
                "opencode-go",
                [
                    {
                        "model_id": "still-here",
                        "protocol": "openai",
                        "protocol_source": "exact_mapping",
                        "capabilities": {},
                        "source_metadata": {},
                    }
                ],
            )
            await service._persist_catalog()  # pyright: ignore[reportPrivateUsage]
        row = await db.fetch_one(
            "SELECT model_id FROM models WHERE model_id = ?", ("still-here",)
        )
        assert row is not None
    finally:
        await db.disconnect()


@pytest.mark.asyncio
async def test_reconcile_clears_stale_account_links() -> None:
    """Disabled account_models rows with no request history are removed."""
    db = Database(path=":memory:")
    await db.connect()
    await MigrationRunner(db).run()
    try:
        config = _config()
        async with httpx.AsyncClient() as client:
            service = CatalogService(config, AccountRegistry(config), db, client)
            # Insert a disabled link with no request history.
            async with db.transaction():
                await db.execute_write(
                    "INSERT INTO accounts "
                    "(name, api_key_env, enabled, weight, provider_id) "
                    "VALUES (?, ?, 1, 1.0, ?)",
                    ("test-acct", "EGGPOOL_TEST_KEY", "opencode-go"),
                )
                account = await db.fetch_one(
                    "SELECT id FROM accounts WHERE name = ?", ("test-acct",)
                )
                assert account is not None
                account_id = int(account["id"])
                await db.execute_write(
                    "INSERT OR REPLACE INTO models "
                    "(model_id, protocol, resolution_status) "
                    "VALUES (?, ?, ?)",
                    ("no-usage", "openai", "resolved"),
                )
                await db.execute_write(
                    "INSERT OR REPLACE INTO account_models "
                    "(account_id, model_id, enabled) VALUES (?, ?, 0)",
                    (account_id, "no-usage"),
                )
            await service._persist_catalog()  # pyright: ignore[reportPrivateUsage]
            link = await db.fetch_one(
                "SELECT model_id FROM account_models "
                "WHERE account_id = ? AND model_id = ?",
                (account_id, "no-usage"),
            )
            assert link is None
    finally:
        await db.disconnect()
