"""Tests for durable model-quarantine scope uniqueness."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import ModelQuarantineRepository

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest_asyncio.fixture()
async def db(tmp_path: pytest.TempPathFactory) -> AsyncGenerator[Database, None]:
    database = Database(path=str(tmp_path / "model_quarantine.sqlite3"))
    await database.connect()
    try:
        await MigrationRunner(database).run()
        yield database
    finally:
        await database.disconnect()


@pytest.mark.asyncio()
async def test_null_upstream_model_id_scope_is_unique(db: Database) -> None:
    repo = ModelQuarantineRepository(db)
    common = {
        "provider_id": "provider-a",
        "account_id": "account-a",
        "canonical_model_id": "canonical-model",
        "upstream_model_id": None,
        "upstream_protocol": "openai",
        "state": "suspected",
        "evidence_provenance": "runtime_http",
        "reason": "model_unavailable",
        "first_observed_epoch": 100.0,
        "last_observed_epoch": 100.0,
        "expiry_epoch": None,
        "last_status_code": 404,
        "last_error_class": "ModelUnavailableError",
    }

    await repo.upsert_observation(observation_count=1, **common)
    await repo.upsert_observation(
        **{
            **common,
            "observation_count": 2,
            "last_observed_epoch": 101.0,
            "reason": "still_unavailable",
        }
    )

    rows = await db.fetch_all(
        "SELECT observation_count, reason FROM model_quarantine "
        "WHERE provider_id = ? AND account_id = ?",
        ("provider-a", "account-a"),
    )
    assert len(rows) == 1
    assert rows[0]["observation_count"] == 2
    assert rows[0]["reason"] == "still_unavailable"
