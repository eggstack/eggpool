"""Tests for Phase 2: case-insensitive batch canonical lookup.

Covers ``ModelInfoRepository.get_canonical_many`` with mixed-case
requested ids and verifies dict key semantics.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.model_info.repository import ModelInfoRepository
from eggpool.model_info.types import CanonicalModelInfo


async def _run_migrations(db: Database) -> None:
    runner = MigrationRunner(db)
    await runner.run()


async def _seed_model(db: Database, model_id: str) -> None:
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO models (model_id, display_name) VALUES (?, ?)",
            (model_id, model_id),
        )


async def _insert_canonical(db: Database, model_id: str) -> CanonicalModelInfo:
    """Seed a canonical row and return it."""
    await _seed_model(db, model_id)
    repo = ModelInfoRepository(db)
    now = datetime.now(UTC)
    info = CanonicalModelInfo(
        model_id=model_id,
        status="partial",
        summary=f"summary for {model_id}",
        sparse=False,
        detail={},
        provenance={"sources": ["provider_catalog"]},
        conflicts={},
        first_seen_at=now,
        last_seen_at=now,
        last_refreshed_at=now,
        next_refresh_at=now + timedelta(hours=1),
    )
    await repo.upsert_canonical(info)
    return info


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_get_canonical_many_case_insensitive_match() -> None:
    """Insert ``GPT-4O``, request ``gpt-4o`` -> result keyed by requested id."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _insert_canonical(db, "GPT-4O")

        repo = ModelInfoRepository(db)
        result = await repo.get_canonical_many(["gpt-4o"])
        assert "gpt-4o" in result
        assert result["gpt-4o"].model_id == "GPT-4O"
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_get_canonical_many_mixed_case_request_keeps_requested_id() -> None:
    """Insert ``mimo-v2.5``, request ``MIMO-V2.5`` -> dict key is ``MIMO-V2.5``."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _insert_canonical(db, "mimo-v2.5")

        repo = ModelInfoRepository(db)
        result = await repo.get_canonical_many(["MIMO-V2.5"])
        assert "MIMO-V2.5" in result
        assert result["MIMO-V2.5"].model_id == "mimo-v2.5"
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_get_canonical_many_empty_list_returns_empty() -> None:
    """Empty list returns ``{}`` and no SQL error."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        repo = ModelInfoRepository(db)
        result = await repo.get_canonical_many([])
        assert result == {}
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_get_canonical_many_none_returns_all_by_stored_id() -> None:
    """None returns dict keyed by stored model_ids."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _insert_canonical(db, "alpha")
        await _insert_canonical(db, "beta")

        repo = ModelInfoRepository(db)
        result = await repo.get_canonical_many(None)
        assert "alpha" in result
        assert "beta" in result
        assert len(result) == 2
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_get_canonical_many_returns_empty_for_unknown_ids() -> None:
    """Requesting ids not in the DB returns empty matches without error."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        repo = ModelInfoRepository(db)
        result = await repo.get_canonical_many(["nonexistent"])
        assert result == {}
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_get_canonical_many_mixed_known_and_unknown() -> None:
    """Only known ids return results; unknown ids are silently skipped."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _insert_canonical(db, "gpt-4o")

        repo = ModelInfoRepository(db)
        result = await repo.get_canonical_many(["gpt-4o", "nonexistent"])
        assert "gpt-4o" in result
        assert "nonexistent" not in result
        assert len(result) == 1
    finally:
        await db.disconnect()
