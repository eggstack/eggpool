"""Tests for migration 0049: model identity match evidence and alias metadata."""

from __future__ import annotations

import json

import pytest

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.model_info.repository import ModelInfoRepository


async def _run_migrations(db: Database) -> None:
    runner = MigrationRunner(db)
    await runner.run()


async def _seed_model(db: Database, model_id: str) -> None:
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO models (model_id, display_name) VALUES (?, ?)",
            (model_id, model_id),
        )


@pytest.mark.asyncio()
async def test_migration_0049_adds_alias_metadata_columns() -> None:
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)

        row = await db.fetch_one("PRAGMA table_info(model_info_aliases)")
        assert row is not None

        rows = await db.fetch_all("PRAGMA table_info(model_info_aliases)")
        columns = {r["name"] for r in rows}
        assert "match_method" in columns
        assert "discovered_by" in columns
        assert "diagnostics_json" in columns

        evidence_rows = await db.fetch_all(
            "PRAGMA table_info(model_info_match_evidence)"
        )
        evidence_columns = {r["name"] for r in evidence_rows}
        assert "model_id" in evidence_columns
        assert "match_method" in evidence_columns
        assert "confidence" in evidence_columns
        assert "diagnostics_json" in evidence_columns
        assert "created_at" in evidence_columns
        assert "last_seen_at" in evidence_columns
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_existing_alias_rows_survive_0049() -> None:
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _seed_model(db, "gpt-4o")

        async with db.transaction():
            await db.execute_write(
                "INSERT INTO model_info_aliases "
                "(model_id, provider_id, alias, source, confidence, active) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("gpt-4o", "openai", "gpt-4o-2024-05-13", "openrouter", 0.9, 1),
            )

        rows = await db.fetch_all("SELECT * FROM model_info_aliases")
        assert len(rows) == 1
        pre_row = dict(rows[0])
        assert pre_row["model_id"] == "gpt-4o"
        assert pre_row["alias"] == "gpt-4o-2024-05-13"

        rows = await db.fetch_all("SELECT * FROM model_info_aliases")
        assert len(rows) == 1
        post_row = dict(rows[0])
        assert post_row["model_id"] == "gpt-4o"
        assert post_row["alias"] == "gpt-4o-2024-05-13"
        assert post_row["confidence"] == 0.9
        assert post_row["active"] == 1
        assert post_row["match_method"] is None
        assert post_row["discovered_by"] is None
        assert post_row["diagnostics_json"] == "{}"
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_upsert_alias_with_method_round_trip() -> None:
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _seed_model(db, "claude-3-opus")

        repo = ModelInfoRepository(db)

        await repo.upsert_alias_with_method(
            "claude-3-opus",
            "anthropic",
            "claude-3-opus-20240229",
            "openrouter",
            match_method="normalized_exact",
            discovered_by="tiered_resolver",
            confidence=0.95,
            diagnostics={"tier": 2, "source_record_id": "sr_abc123"},
        )

        aliases = await repo.get_aliases_for_model("claude-3-opus")
        assert "claude-3-opus-20240229" in aliases

        rows = await db.fetch_all(
            "SELECT match_method, discovered_by, diagnostics_json, confidence "
            "FROM model_info_aliases "
            "WHERE model_id = ? AND alias = ? AND source = ?",
            ("claude-3-opus", "claude-3-opus-20240229", "openrouter"),
        )
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["match_method"] == "normalized_exact"
        assert row["discovered_by"] == "tiered_resolver"
        assert row["confidence"] == 0.95
        diag = json.loads(row["diagnostics_json"])
        assert diag["tier"] == 2
        assert diag["source_record_id"] == "sr_abc123"

        await repo.upsert_alias_with_method(
            "claude-3-opus",
            "anthropic",
            "claude-3-opus-20240229",
            "openrouter",
            match_method="similarity_guarded",
            discovered_by="test_override",
            confidence=0.75,
            diagnostics={"updated": True},
        )

        rows = await db.fetch_all(
            "SELECT match_method, discovered_by, diagnostics_json, confidence "
            "FROM model_info_aliases "
            "WHERE model_id = ? AND alias = ? AND source = ?",
            ("claude-3-opus", "claude-3-opus-20240229", "openrouter"),
        )
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["match_method"] == "similarity_guarded"
        assert row["discovered_by"] == "test_override"
        assert row["confidence"] == 0.75
        diag = json.loads(row["diagnostics_json"])
        assert diag["updated"] is True
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_legacy_upsert_alias_does_not_fail_after_0049() -> None:
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _seed_model(db, "gemini-1.5-pro")

        repo = ModelInfoRepository(db)

        await repo.upsert_alias(
            "gemini-1.5-pro",
            "google",
            "gemini-1.5-pro-latest",
            "openrouter",
            confidence=0.8,
            active=True,
        )

        aliases = await repo.get_aliases_for_model("gemini-1.5-pro")
        assert "gemini-1.5-pro-latest" in aliases

        rows = await db.fetch_all(
            "SELECT match_method, discovered_by, diagnostics_json, confidence "
            "FROM model_info_aliases "
            "WHERE model_id = ? AND alias = ? AND source = ?",
            ("gemini-1.5-pro", "gemini-1.5-pro-latest", "openrouter"),
        )
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["confidence"] == 0.8
        assert row["match_method"] is None
        assert row["discovered_by"] is None
        assert row["diagnostics_json"] == "{}"
    finally:
        await db.disconnect()
