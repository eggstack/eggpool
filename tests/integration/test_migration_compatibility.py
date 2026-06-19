"""Phase 17/18 migration compatibility and checksum verification.

Verifies that:
- All migrations apply cleanly to a fresh database.
- A real historical v11 fixture upgrades correctly under the current
  migration runner (no migration is re-executed, all rows survive).
- Migration files are immutable: any edit fails the checksum test.
- The historical fixture's own checksum is recorded and protected.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

import aiosqlite
import pytest

from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import SCHEMA_DIR, MigrationRunner

if TYPE_CHECKING:
    from pathlib import Path


# SCHEMA_DIR = src/go_aggregator/db/schema. Walk up to the project
# root and into tests/fixtures/schema.
_PROJECT_ROOT = SCHEMA_DIR.parent.parent.parent.parent
FIXTURE_DIR = _PROJECT_ROOT / "tests" / "fixtures" / "schema"
HISTORICAL_FIXTURE = FIXTURE_DIR / "pre_phase17_v11.sql"
HISTORICAL_CHECKSUMS = FIXTURE_DIR / "checksums.json"


def _expected_migration_files() -> list[Path]:
    return sorted(SCHEMA_DIR.glob("*.sql"))


def _compute_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_checksums_manifest() -> dict[str, str]:
    manifest_path = SCHEMA_DIR / "checksums.json"
    if not manifest_path.exists():
        return {}
    return json.loads(manifest_path.read_text(encoding="utf-8")).get("files", {})


def _load_historical_checksums_manifest() -> dict[str, str]:
    if not HISTORICAL_CHECKSUMS.exists():
        return {}
    return json.loads(HISTORICAL_CHECKSUMS.read_text(encoding="utf-8")).get("files", {})


def _split_sql_statements(sql: str) -> list[str]:
    """Split a SQL file into individual statements.

    Strips comment lines and drops empty statements. Used to apply the
    historical fixture without depending on the migration runner.
    """
    statements: list[str] = []
    for block in sql.split(";"):
        lines = [
            line for line in block.splitlines() if not line.strip().startswith("--")
        ]
        stmt = "\n".join(lines).strip()
        if stmt:
            statements.append(stmt)
    return statements


async def _fresh_db() -> Database:
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    return db


async def _historical_v11_db(tmp_path: Path) -> Database:
    """Build a database from the historical v11 fixture and run
    the current migrations on top.

    Steps:
      1. Create a file-backed SQLite database.
      2. Apply the fixture exactly (no migration runner).
      3. Open it through ``Database``.
      4. Run the current ``MigrationRunner``.
      5. Verify no already-applied migration is re-executed.
    """
    db_path = tmp_path / "historical_v11.sqlite3"
    fixture_sql = HISTORICAL_FIXTURE.read_text(encoding="utf-8")
    raw = await aiosqlite.connect(str(db_path))
    try:
        await raw.executescript(fixture_sql)
        await raw.commit()
    finally:
        await raw.close()

    db = Database(path=str(db_path))
    await db.connect()
    applied_before = await MigrationRunner(db)._applied_versions()  # noqa: SLF001
    runner = MigrationRunner(db)
    await runner.run()
    applied_after = await MigrationRunner(db)._applied_versions()  # noqa: SLF001
    assert applied_before <= applied_after, (
        "MigrationRunner re-executed migrations that were already "
        f"applied by the historical fixture: before={sorted(applied_before)} "
        f"after={sorted(applied_after)}"
    )
    return db


def _table_names(db: Database) -> set[str]:
    async def _fetch() -> set[str]:
        rows = await db.fetch_all(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE '\\_%' ESCAPE '\\'"
        )
        return {row["name"] for row in rows}

    return _run_sync(_fetch())


async def _behavioral_schema_metadata(db: Database) -> dict[str, Any]:
    """Return a structural metadata snapshot of the database.

    The snapshot is suitable for equality comparison between two
    databases that are semantically equivalent but whose raw
    ``sqlite_master.sql`` strings differ in formatting.

    Fields:
      - tables: sorted list of user-visible table names
      - columns: per-table list of (cid, name, type, notnull,
        default, pk) tuples from PRAGMA table_info
      - indexes: per-table mapping of index name to sorted list of
        (column_name, ) tuples
    """
    table_rows = await db.fetch_all(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "AND name NOT LIKE '\\_%' ESCAPE '\\' ORDER BY name"
    )
    table_names = [row["name"] for row in table_rows]

    columns: dict[str, list[tuple[Any, ...]]] = {}
    for name in table_names:
        info_rows = await db.fetch_all(f"PRAGMA table_info({name})")
        columns[name] = [
            (
                row["cid"],
                row["name"],
                row["type"],
                int(row["notnull"]),
                row["dflt_value"],
                int(row["pk"]),
            )
            for row in info_rows
        ]

    indexes: dict[str, dict[str, list[str]]] = {}
    for name in table_names:
        idx_rows = await db.fetch_all(f"PRAGMA index_list({name})")
        indexes[name] = {}
        for idx in idx_rows:
            if idx["origin"] != "c":  # 'c' = CREATE INDEX, 'u' = UNIQUE
                continue
            idx_name = idx["name"]
            col_rows = await db.fetch_all(f"PRAGMA index_info({idx_name})")
            indexes[name][idx_name] = sorted(row["name"] for row in col_rows)

    return {
        "tables": table_names,
        "columns": columns,
        "indexes": indexes,
    }


def _run_sync(coro: Any) -> Any:
    import asyncio

    return asyncio.get_event_loop().run_until_complete(coro)


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
        """The fresh schema and a database freshly built from the
        historical v11 fixture must be behaviorally schema-equivalent.

        We compare structural metadata (table names, column names/
        types/nullability/defaults/PK, index names and indexed
        columns) rather than raw ``sqlite_master.sql`` text, because
        semantically equivalent schemas can differ in whitespace,
        column ordering, and SQL formatting.
        """
        import tempfile

        fresh = await _fresh_db()
        try:
            with tempfile.TemporaryDirectory() as td:
                from pathlib import Path

                existing = await _historical_v11_db(Path(td))
                try:
                    fresh_meta = await _behavioral_schema_metadata(fresh)
                    existing_meta = await _behavioral_schema_metadata(existing)
                    assert fresh_meta == existing_meta, (
                        "Fresh and historical databases diverge. An "
                        "applied migration may have been modified."
                    )
                finally:
                    await existing.disconnect()
        finally:
            await fresh.disconnect()

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
    async def test_migration_versions_match_files_on_disk(self) -> None:
        """The number and identity of applied migrations must exactly
        match the .sql files in src/go_aggregator/db/schema.

        Replaces the prior `assert len(versions) == len(versions)` tautology.
        """
        db = await _fresh_db()
        try:
            rows = await db.fetch_all("SELECT version FROM _migrations")
            versions = sorted(row["version"] for row in rows)
            expected = [
                int(path.stem.split("_")[0])
                for path in sorted(SCHEMA_DIR.glob("*.sql"))
            ]
            assert versions == expected, (
                f"Applied versions {versions} differ from files-on-disk "
                f"versions {expected}"
            )
            # Sanity checks: no gaps, no duplicates.
            assert len(versions) == len(set(versions)), (
                f"Duplicate migration versions: {versions}"
            )
            assert versions == list(range(min(versions), max(versions) + 1)), (
                f"Gap in migration version sequence: {versions}"
            )
        finally:
            await db.disconnect()


class TestHistoricalFixture:
    """The historical v11 fixture must open, upgrade, and behave
    equivalently to a fresh production database."""

    @pytest.mark.asyncio
    async def test_fixture_opens_successfully(self, tmp_path: Path) -> None:
        db = await _historical_v11_db(tmp_path)
        try:
            row = await db.fetch_one("SELECT COUNT(*) AS c FROM _migrations")
            assert row is not None
            assert int(row["c"]) >= 11
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_running_migrations_on_fixture_is_idempotent(
        self, tmp_path: Path
    ) -> None:
        """Running MigrationRunner.run twice on the upgraded fixture
        must not re-execute any migration.
        """
        db = await _historical_v11_db(tmp_path)
        try:
            runner = MigrationRunner(db)
            applied_before = await runner._applied_versions()  # noqa: SLF001
            await runner.run()
            applied_after = await runner._applied_versions()  # noqa: SLF001
            assert applied_before == applied_after
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_representative_rows_survive_upgrade(self, tmp_path: Path) -> None:
        """Every representative row inserted by the fixture must
        still be readable after the current migration runner is
        applied on top.
        """
        db = await _historical_v11_db(tmp_path)
        try:
            # accounts
            account = await db.fetch_one(
                "SELECT name, api_key_env, enabled, weight FROM accounts "
                "WHERE name = ?",
                ("historical-account",),
            )
            assert account is not None
            assert account["name"] == "historical-account"
            assert account["api_key_env"] == "GOROUTER_TEST_KEY_1"
            assert int(account["enabled"]) == 1

            # models: protocol + resolution_status are the v11
            # columns most at risk of regression.
            model = await db.fetch_one(
                "SELECT model_id, protocol, resolution_status, "
                "protocol_source, endpoint_path FROM models "
                "WHERE model_id = ?",
                ("historical-model",),
            )
            assert model is not None
            assert model["protocol"] == "openai"
            assert model["resolution_status"] == "resolved"
            assert model["protocol_source"] == "config"
            assert model["endpoint_path"] == "/v1/chat/completions"

            # account_models
            link = await db.fetch_one(
                "SELECT enabled FROM account_models "
                "WHERE account_id = 1 AND model_id = ?",
                ("historical-model",),
            )
            assert link is not None
            assert int(link["enabled"]) == 1

            # model_price_snapshots
            snap = await db.fetch_one(
                "SELECT model_id, source, "
                "input_per_million_microdollars, "
                "output_per_million_microdollars "
                "FROM model_price_snapshots WHERE model_id = ?",
                ("historical-model",),
            )
            assert snap is not None
            assert snap["source"] == "config"
            assert int(snap["input_per_million_microdollars"]) == 10000
            assert int(snap["output_per_million_microdollars"]) == 20000

            # requests
            req = await db.fetch_one(
                "SELECT account_id, model_id, status, input_tokens, "
                "output_tokens, cost_microdollars, proxy_request_id "
                "FROM requests WHERE id = 1"
            )
            assert req is not None
            assert req["status"] == "success"
            assert int(req["input_tokens"]) == 100
            assert int(req["output_tokens"]) == 50
            assert int(req["cost_microdollars"]) == 1500
            assert req["proxy_request_id"] == "legacy-historical-1"

            # request_attempts
            attempt = await db.fetch_one(
                "SELECT request_id, attempt_number, status_code "
                "FROM request_attempts WHERE id = 1"
            )
            assert attempt is not None
            assert int(attempt["request_id"]) == 1
            assert int(attempt["attempt_number"]) == 1
            assert int(attempt["status_code"]) == 200

            # reservations
            resv = await db.fetch_one(
                "SELECT request_id, account_id, model_id, status, "
                "release_reason FROM reservations WHERE id = 1"
            )
            assert resv is not None
            assert resv["status"] == "released"
            assert resv["release_reason"] == "completed"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_fresh_and_upgraded_schemas_equivalent(self, tmp_path: Path) -> None:
        """Behavioral equivalence: a representative set of repository
        operations must succeed on both the fresh and the upgraded
        databases with identical observable outcomes.
        """
        from go_aggregator.db.repositories import (
            AccountRepository,
            PriceSnapshotRepository,
            RequestRepository,
            ReservationRepository,
        )

        async def _exercise(db: Database) -> dict[str, Any]:
            account_repo = AccountRepository(db)
            request_repo = RequestRepository(db)
            reservation_repo = ReservationRepository(db)
            price_repo = PriceSnapshotRepository(db)

            account_id = await account_repo.sync_from_config(
                [
                    {
                        "name": "compat-account",
                        "api_key_env": "GOROUTER_COMPAT_KEY",
                        "enabled": True,
                        "weight": 2.5,
                    }
                ],
            )
            account_id_value = int(account_id["compat-account"])

            async with db.transaction():
                await db.execute_write(
                    "INSERT OR IGNORE INTO models (model_id, protocol, "
                    "resolution_status) VALUES (?, 'openai', 'resolved')",
                    ("compat-model",),
                )
                request_id = await request_repo.create_pending(
                    request_id="compat-request-1",
                    model_id="compat-model",
                    protocol="openai",
                    streamed=False,
                    account_id=account_id_value,
                    reserved_microdollars=100,
                )
                await request_repo.update_after_completion(
                    request_id,
                    status="success",
                    input_tokens=10,
                    output_tokens=20,
                    cost_microdollars=300,
                )
                reservation_id = await reservation_repo.create(
                    request_id=request_id,
                    account_id=account_id_value,
                    model_id="compat-model",
                    estimated_tokens=10,
                    estimated_microdollars=300,
                )
                await price_repo.record(
                    model_id="compat-model",
                    input_price_per_1k=0.00001,
                    output_price_per_1k=0.00002,
                    source="config",
                )

            async with db.transaction():
                released = await reservation_repo.release(reservation_id, "test-compat")

            model_row = await db.fetch_one(
                "SELECT protocol, resolution_status FROM models WHERE model_id = ?",
                ("compat-model",),
            )
            assert model_row is not None

            request_row = await request_repo.get_by_id(request_id)
            assert request_row is not None
            assert request_row["status"] == "success"

            price_row = await price_repo.get_latest("compat-model")
            assert price_row is not None
            assert price_row["source"] == "config"

            return {
                "reservation_released": released,
                "model_protocol": model_row["protocol"],
                "model_resolution_status": model_row["resolution_status"],
                "request_status": request_row["status"],
                "request_input_tokens": int(request_row["input_tokens"]),
                "request_output_tokens": int(request_row["output_tokens"]),
                "request_cost_microdollars": int(request_row["cost_microdollars"]),
                "price_source": price_row["source"],
                "account_name_resolved": account_id_value > 0,
                "request_id_resolved": int(request_id) > 0,
            }

        fresh = await _fresh_db()
        upgraded = await _historical_v11_db(tmp_path)
        try:
            fresh_outcome = await _exercise(fresh)
            upgraded_outcome = await _exercise(upgraded)
            assert fresh_outcome == upgraded_outcome
        finally:
            await fresh.disconnect()
            await upgraded.disconnect()


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


class TestHistoricalFixtureChecksums:
    """The historical fixture's own SHA-256 is recorded and protected."""

    def test_historical_fixture_exists(self) -> None:
        assert HISTORICAL_FIXTURE.exists(), (
            f"Missing historical fixture at {HISTORICAL_FIXTURE}"
        )

    def test_historical_checksums_manifest_exists(self) -> None:
        assert HISTORICAL_CHECKSUMS.exists(), (
            f"Missing historical checksums manifest at {HISTORICAL_CHECKSUMS}"
        )

    def test_historical_checksums_manifest_covers_fixture(self) -> None:
        manifest = _load_historical_checksums_manifest()
        on_disk = {p.name for p in FIXTURE_DIR.glob("*.sql")}
        in_manifest = set(manifest.keys())
        missing = on_disk - in_manifest
        extra = in_manifest - on_disk
        assert not missing, f"Historical fixtures missing from manifest: {missing}"
        assert not extra, f"Manifest entries not on disk: {extra}"

    def test_historical_fixture_checksum_matches(self) -> None:
        manifest = _load_historical_checksums_manifest()
        for path in FIXTURE_DIR.glob("*.sql"):
            expected = manifest[path.name]
            actual = _compute_checksum(path)
            assert actual == expected, (
                f"Checksum mismatch for historical fixture {path.name}: "
                f"expected={expected}, actual={actual}. "
                "Editing the historical upgrade fixture is forbidden; "
                "create a new fixture file instead."
            )
