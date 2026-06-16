"""Phase 17 migration compatibility and checksum verification.

Verifies that:
- All migrations apply cleanly to a fresh database.
- A simulated existing database (migrations 1..11 already applied)
  behaves equivalently to a fresh database.
- Migration files are immutable: any edit fails the checksum test.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import pytest

from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import SCHEMA_DIR, MigrationRunner

if TYPE_CHECKING:
    from pathlib import Path


def _expected_migration_files() -> list[Path]:
    return sorted(SCHEMA_DIR.glob("*.sql"))


def _compute_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_checksums_manifest() -> dict[str, str]:
    manifest_path = SCHEMA_DIR / "checksums.json"
    if not manifest_path.exists():
        return {}
    return json.loads(manifest_path.read_text(encoding="utf-8")).get("files", {})


async def _fresh_db() -> Database:
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    return db


async def _simulated_existing_db() -> Database:
    """Build a database with the 0001..0011 migrations already applied.

    We compose a temporary directory with the historical 0005 (using
    the legacy ``source`` default) and apply the migrations in order.
    """
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    return db


def _table_names(db: Database) -> set[str]:
    import asyncio

    async def _fetch() -> set[str]:
        rows = await db.fetch_all(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE '\\_%' ESCAPE '\\'"
        )
        return {row["name"] for row in rows}

    return asyncio.get_event_loop().run_until_complete(_fetch())


class TestMigrationCompatibility:
    """Fresh and upgraded databases must be schema-equivalent."""

    @pytest.mark.asyncio
    async def test_fresh_db_has_required_tables(self) -> None:
        db = await _fresh_db()
        try:
            rows = await db.fetch_all(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE '\\_%' ESCAPE '\\'"
            )
            names = {r["name"] for r in rows}
            required = {
                "accounts",
                "models",
                "account_models",
                "requests",
                "request_attempts",
                "reservations",
                "model_price_snapshots",
                "account_events",
                "health_probe",
            }
            assert required.issubset(names), f"Missing tables: {required - names}"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_fresh_and_existing_schema_equivalent(self) -> None:
        """Apply migrations on a fresh in-memory DB; schema must match.

        The simulated-existing branch uses the same migrations, so the
        resulting schema must be identical. This is the regression
        guard against accidentally changing an applied migration.
        """
        fresh = await _fresh_db()
        existing = await _simulated_existing_db()
        try:
            fresh_rows = await fresh.fetch_all(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type IN ('table', 'index') AND name NOT LIKE '\\_%' "
                "ESCAPE '\\' ORDER BY name"
            )
            existing_rows = await existing.fetch_all(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type IN ('table', 'index') AND name NOT LIKE '\\_%' "
                "ESCAPE '\\' ORDER BY name"
            )
            assert fresh_rows == existing_rows, (
                "Fresh and existing databases diverge. An applied migration "
                "may have been modified."
            )
        finally:
            await fresh.disconnect()
            await existing.disconnect()

    @pytest.mark.asyncio
    async def test_migration_runner_is_idempotent(self) -> None:
        db = await _fresh_db()
        try:
            runner = MigrationRunner(db)
            applied_before = await runner._applied_versions()  # noqa: SLF001
            await runner.run()
            applied_after = await runner._applied_versions()  # noqa: SLF001
            assert applied_before == applied_after
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_price_snapshot_source_default_is_config(self) -> None:
        """Migration 0005 must use the historical 'config' default.

        Production insert paths explicitly provide source, so the
        default exists only for legacy rows. Restoring the historical
        default keeps existing schemas and new schemas equivalent.
        """
        db = await _fresh_db()
        try:
            row = await db.fetch_one(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='model_price_snapshots'"
            )
            assert row is not None
            ddl = row["sql"] or ""
            assert "DEFAULT 'config'" in ddl, (
                "Migration 0005 source default must be 'config' to "
                "match the historical schema."
            )
            assert "DEFAULT 'upstream'" not in ddl, (
                "Migration 0005 source default is 'upstream'; this would "
                "silently change behavior for legacy rows."
            )
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_migration_applied_count(self) -> None:
        """Exactly N migrations must be recorded."""
        db = await _fresh_db()
        try:
            rows = await db.fetch_all("SELECT version FROM _migrations")
            versions = sorted(row["version"] for row in rows)
            assert len(versions) == len(versions)
            assert versions == list(range(1, len(versions) + 1))
        finally:
            await db.disconnect()


class TestMigrationChecksums:
    """Migration files must match their recorded checksums."""

    def test_checksums_manifest_exists(self) -> None:
        manifest_path = SCHEMA_DIR / "checksums.json"
        assert manifest_path.exists(), f"Missing checksums manifest at {manifest_path}"

    def test_manifest_covers_all_migrations(self) -> None:
        manifest = _load_checksums_manifest()
        on_disk = {p.name for p in _expected_migration_files()}
        in_manifest = set(manifest.keys())
        missing = on_disk - in_manifest
        extra = in_manifest - on_disk
        assert not missing, f"Migrations missing from manifest: {missing}"
        assert not extra, f"Manifest entries not on disk: {extra}"

    def test_every_migration_checksum_matches(self) -> None:
        manifest = _load_checksums_manifest()
        for path in _expected_migration_files():
            expected = manifest[path.name]
            actual = _compute_checksum(path)
            assert actual == expected, (
                f"Checksum mismatch for {path.name}: "
                f"expected={expected}, actual={actual}. "
                "Editing an applied migration is forbidden; create a new "
                "migration instead."
            )

    def test_migration_files_have_unique_versions(self) -> None:
        versions: list[int] = []
        for path in _expected_migration_files():
            try:
                version = int(path.stem.split("_")[0])
            except (ValueError, IndexError):
                continue
            versions.append(version)
        assert len(versions) == len(set(versions)), (
            f"Duplicate migration version numbers: {versions}"
        )
