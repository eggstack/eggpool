"""Phase 18 regression matrix.

This file is the cross-cutting release gate described in Section 9
of the Phase 18 plan. Each test class corresponds to one letter
in the matrix (A through H) and ensures the high-level invariant
without re-running the detailed unit-level coverage already
exercised elsewhere in the test suite.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import SCHEMA_DIR, MigrationRunner
from go_aggregator.db.repositories import (
    AccountRepository,
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
)

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "schema"
FIXTURE_SQL = FIXTURE_DIR / "pre_phase17_v11.sql"
FIXTURE_CHECKSUMS = FIXTURE_DIR / "checksums.json"


def _make_selected(db_id: str, attempt_id: int, reservation_id: str) -> object:
    """Build a minimal finalizer ``selected`` stand-in outside any class body.

    The previous nested-class form hit Python's class-scope visibility
    rules when assigning captured locals; this helper sidesteps the
    issue while preserving the call shape.
    """

    class _Selected:
        pass

    sel = _Selected()
    sel.db_request_id = db_id
    sel.account_name = "priv-acct"
    sel.model_id = "gpt-4"
    sel.attempt_id = attempt_id
    sel.reservation_id = reservation_id
    sel.estimated_microdollars = 10_000
    sel.attempt_number = 1
    return sel


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# A. Checker fail-closed behavior
# ---------------------------------------------------------------------------


class TestACheckerFailClosed:
    def test_empty_sqlite_file_returns_2(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.sqlite3"
        empty.write_bytes(b"")
        old = os.environ.get("GOROUTER_DB_PATH")
        os.environ["GOROUTER_DB_PATH"] = str(empty)
        try:
            from scripts import check_database

            code = check_database.main_sync()
        finally:
            if old is None:
                os.environ.pop("GOROUTER_DB_PATH", None)
            else:
                os.environ["GOROUTER_DB_PATH"] = old
        assert code == 2

    def test_valid_schema_returns_0(self, tmp_path: Path) -> None:
        db_path = tmp_path / "valid.sqlite3"
        db = Database(path=str(db_path))
        import asyncio

        async def _setup() -> None:
            await db.connect()
            runner = MigrationRunner(db)
            await runner.run()
            await db.disconnect()

        asyncio.run(_setup())
        old = os.environ.get("GOROUTER_DB_PATH")
        os.environ["GOROUTER_DB_PATH"] = str(db_path)
        try:
            from scripts import check_database

            code = check_database.main_sync()
        finally:
            if old is None:
                os.environ.pop("GOROUTER_DB_PATH", None)
            else:
                os.environ["GOROUTER_DB_PATH"] = old
        assert code == 0

    def test_invariant_violation_returns_1(self, tmp_path: Path) -> None:
        """An old 'pending' request beyond threshold produces exit 1."""
        db_path = tmp_path / "stale.sqlite3"
        db = Database(path=str(db_path))
        import asyncio

        async def _setup() -> None:
            await db.connect()
            runner = MigrationRunner(db)
            await runner.run()
            async with db.transaction():
                await db.execute_write(
                    "INSERT INTO accounts (name, api_key_env) VALUES (?, ?)",
                    ("stale-acct", "STALE_KEY"),
                )
                await db.execute_write(
                    "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
                    ("stale-model", "openai"),
                )
                await db.execute_write(
                    "INSERT INTO requests (account_id, model_id, status, "
                    "started_at, protocol) VALUES (?, ?, 'pending', "
                    "datetime('now', '-2 hours'), 'openai')",
                    (1, "stale-model"),
                )
            await db.disconnect()

        asyncio.run(_setup())
        old = os.environ.get("GOROUTER_DB_PATH")
        os.environ["GOROUTER_DB_PATH"] = str(db_path)
        try:
            from scripts import check_database

            code = check_database.main_sync()
        finally:
            if old is None:
                os.environ.pop("GOROUTER_DB_PATH", None)
            else:
                os.environ["GOROUTER_DB_PATH"] = old
        assert code == 1


# ---------------------------------------------------------------------------
# B. Historical upgrade
# ---------------------------------------------------------------------------


class TestBHistoricalUpgrade:
    def test_load_historical_fixture_reopen_and_upgrade(self, tmp_path: Path) -> None:
        """Apply the v11 fixture, reopen through Database, run migrations,
        and verify representative rows survive."""
        db_path = tmp_path / "historical.sqlite3"
        import asyncio

        async def _exercise() -> None:
            conn = sqlite3.connect(str(db_path))
            try:
                conn.executescript(FIXTURE_SQL.read_text(encoding="utf-8"))
                conn.commit()
            finally:
                conn.close()

            db = Database(path=str(db_path))
            await db.connect()
            try:
                runner = MigrationRunner(db)
                applied_before = await runner._applied_versions()
                await runner.run()
                applied_after = await runner._applied_versions()
                assert applied_before == applied_after

                row = await db.fetch_one(
                    "SELECT name FROM accounts WHERE name = ?",
                    ("historical-account",),
                )
                assert row is not None
                model_row = await db.fetch_one(
                    "SELECT model_id, protocol, resolution_status "
                    "FROM models WHERE model_id = ?",
                    ("historical-model",),
                )
                assert model_row is not None
                assert model_row["protocol"] == "openai"
                assert model_row["resolution_status"] == "resolved"
            finally:
                await db.disconnect()

        asyncio.run(_exercise())


# ---------------------------------------------------------------------------
# C. CLI catalog refresh
# ---------------------------------------------------------------------------


class TestCCatalogRefresh:
    def test_refresh_creates_accounts_and_models(self, tmp_path: Path) -> None:
        """End-to-end: a fresh database with two accounts refreshes into a
        persisted catalog."""
        import asyncio

        import httpx
        import respx

        from go_aggregator.accounts.registry import (
            AccountRegistry,
            account_config_rows,
        )
        from go_aggregator.catalog.service import CatalogService
        from go_aggregator.models.config import AppConfig

        db_path = tmp_path / "refresh.sqlite3"
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            f"""
[server]
api_key_env = "REFRESH_TEST_KEY"
[upstream]
base_url = "https://test-upstream.example.com"
[database]
path = "{db_path}"
[models]
refresh_interval_s = 0
startup_refresh = true
[dashboard]
enabled = false
[[accounts]]
name = "alpha"
api_key_env = "REFRESH_TEST_KEY"
enabled = true
weight = 1.0
[[accounts]]
name = "beta"
api_key_env = "REFRESH_TEST_KEY"
enabled = true
weight = 1.0
""",
            encoding="utf-8",
        )
        os.environ["REFRESH_TEST_KEY"] = "synthetic-key"

        async def _exercise() -> None:
            config = AppConfig.from_toml(str(config_path))
            db = Database(path=str(db_path))
            await db.connect()
            try:
                runner = MigrationRunner(db)
                await runner.run()
                account_repo = AccountRepository(db)
                await account_repo.sync_from_config(account_config_rows(config), db)
                registry = AccountRegistry(config)
                with respx.mock:
                    respx.get("https://test-upstream.example.com/models").mock(
                        return_value=httpx.Response(
                            200,
                            json={
                                "object": "list",
                                "data": [{"id": "gpt-4", "object": "model"}],
                            },
                        )
                    )
                    async with httpx.AsyncClient(
                        base_url=config.upstream.base_url
                    ) as client:
                        catalog = CatalogService(config, registry, db, client)
                        await catalog.refresh()
                accounts = await db.fetch_all("SELECT name FROM accounts ORDER BY name")
                assert [r["name"] for r in accounts] == ["alpha", "beta"]
                am = await db.fetch_all(
                    "SELECT a.name AS name, m.model_id "
                    "FROM account_models am "
                    "JOIN accounts a ON a.id = am.account_id "
                    "JOIN models m ON m.model_id = am.model_id "
                    "WHERE am.enabled = 1 ORDER BY a.name"
                )
                assert {(r["name"], r["model_id"]) for r in am} == {
                    ("alpha", "gpt-4"),
                    ("beta", "gpt-4"),
                }
            finally:
                await db.disconnect()

        asyncio.run(_exercise())


# ---------------------------------------------------------------------------
# D. Maintenance API
# ---------------------------------------------------------------------------


class TestDMaintenceAPI:
    def test_vacuum_reopen_and_data_intact(self, tmp_path: Path) -> None:
        """Database.vacuum() reopens cleanly and preserves data."""
        import asyncio

        db_path = tmp_path / "maint.sqlite3"

        async def _exercise() -> None:
            db = Database(path=str(db_path))
            await db.connect()
            try:
                runner = MigrationRunner(db)
                await runner.run()
                async with db.transaction():
                    await db.execute_write(
                        "INSERT INTO accounts (name, api_key_env) VALUES (?, ?)",
                        ("vac-acct", "VAC_KEY"),
                    )
                await db.vacuum()
            finally:
                await db.disconnect()

            reopened = Database(path=str(db_path))
            await reopened.connect()
            try:
                row = await reopened.fetch_one(
                    "SELECT name FROM accounts WHERE name = ?",
                    ("vac-acct",),
                )
                assert row is not None
            finally:
                await reopened.disconnect()

        asyncio.run(_exercise())


# ---------------------------------------------------------------------------
# E. Privacy
# ---------------------------------------------------------------------------


class TestEPrivacyAllowlist:
    def test_enabled_persistence_drops_unknown_payload_keys(self) -> None:
        """With persist_redacted_error_detail enabled, unknown keys
        such as ``payload`` are dropped from the persisted detail."""
        import asyncio

        from go_aggregator.request.finalizer import (
            FinalizationData,
            FinalizationOutcome,
            RequestFinalizer,
        )

        secret_bearing = json.dumps(
            {
                "type": "invalid_request",
                "message": "bad token sk-supersecret",
                "payload": "private source code",
                "data": "should-be-dropped",
            }
        )

        async def _exercise() -> None:
            db = Database(path=":memory:")
            await db.connect()
            try:
                runner = MigrationRunner(db)
                await runner.run()
                async with db.transaction():
                    await db.execute_write(
                        "INSERT INTO accounts (name, api_key_env) VALUES (?, ?)",
                        ("priv-acct", "PRIV_KEY"),
                    )
                    await db.execute_write(
                        "INSERT OR IGNORE INTO models (model_id, protocol) "
                        "VALUES (?, ?)",
                        ("gpt-4", "openai"),
                    )
                request_repo = RequestRepository(db)
                attempt_repo = AttemptRepository(db)
                reservation_repo = ReservationRepository(db)
                async with db.transaction():
                    db_id = await request_repo.create_pending(
                        request_id="priv-req",
                        model_id="gpt-4",
                        protocol="openai",
                        streamed=False,
                        account_id=1,
                    )
                    attempt_id = await attempt_repo.create(
                        request_id=db_id,
                        attempt_number=1,
                        account_id=1,
                    )
                    reservation_id = await reservation_repo.create(
                        request_id=db_id,
                        account_id=1,
                        model_id="gpt-4",
                        estimated_tokens=100,
                        estimated_microdollars=10_000,
                    )

                finalizer = RequestFinalizer(
                    db=db,
                    request_repo=request_repo,
                    attempt_repo=attempt_repo,
                    reservation_repo=reservation_repo,
                    persist_error_detail=True,
                )

                sel = _make_selected(db_id, attempt_id, reservation_id)
                await finalizer.finalize(
                    sel,
                    FinalizationData(
                        outcome=FinalizationOutcome.UPSTREAM_ERROR,
                        error_class="UpstreamError",
                        error_detail=secret_bearing,
                    ),
                )

                row = await db.fetch_one(
                    "SELECT error_detail FROM requests WHERE id = ?",
                    (db_id,),
                )
                detail = row["error_detail"]
                assert detail is not None
                for forbidden in (
                    "sk-supersecret",
                    "private source code",
                    "should-be-dropped",
                    '"payload"',
                    '"data"',
                ):
                    assert forbidden not in detail, forbidden
                assert '"type"' in detail
                assert '"message"' in detail
            finally:
                await db.disconnect()

        asyncio.run(_exercise())


# ---------------------------------------------------------------------------
# F. Streaming diagnostics
# ---------------------------------------------------------------------------


class TestFStreamingDiagnostics:
    def test_fragmented_marker_is_recognized(self) -> None:
        """scripts.smoke_test handles markers split across chunks."""
        import io

        import httpx
        import smoke_test

        chunks = [b"da", b"ta: {}\n\n"]
        httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "x-proxy-request-id": "req-frag",
                "x-proxy-attempt-count": "1",
            },
            content=io.BytesIO(b"".join(chunks)),
        )
        scanner = smoke_test._RollingMarkerScanner(b"data:")
        seen = scanner.feed(b"")
        for chunk in chunks:
            seen = seen or scanner.feed(chunk)
        assert seen is True

    def test_missing_proxy_metadata_fails(self) -> None:
        """A non-streaming call missing x-proxy-attempt-count fails."""
        import httpx
        import smoke_test

        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"id": "x", "choices": [], "usage": {}},
                headers={
                    "x-proxy-request-id": "req-test",
                    # missing x-proxy-attempt-count
                },
            )
        )
        client = httpx.Client(transport=transport, timeout=5.0)
        try:
            result = smoke_test._openai(client, "http://stub", "k", "gpt-4")
        finally:
            client.close()
        assert not result.ok
        assert "attempt-count" in result.detail


# ---------------------------------------------------------------------------
# G. CI configuration
# ---------------------------------------------------------------------------


class TestGCIConfiguration:
    def test_ci_workflow_includes_scripts_in_ruff(self) -> None:
        workflow = (
            Path(__file__).parent.parent.parent / ".github" / "workflows" / "ci.yml"
        )
        contents = workflow.read_text(encoding="utf-8")
        assert "scripts/" in contents

    def test_pyright_includes_scripts(self) -> None:
        pyproject = Path(__file__).parent.parent.parent / "pyproject.toml"
        contents = pyproject.read_text(encoding="utf-8")
        assert re.search(r'include\s*=\s*\[\s*"src"\s*,\s*"scripts"\s*\]', contents), (
            "pyproject.toml must include scripts in pyright include paths"
        )
        # The scripts/ directory must NOT appear in the pyright exclude
        # list (we may still exclude tests).
        pyright_section = contents.split("[tool.pyright]")[1].split("[")[0]
        assert "exclude" not in pyright_section or '"scripts"' not in re.search(
            r"exclude\s*=\s*\[([^\]]*)\]", pyright_section
        ).group(1)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# H. Migration integrity
# ---------------------------------------------------------------------------


class TestHMigrationIntegrity:
    def test_migration_manifest_covers_all_migrations(self) -> None:
        manifest_path = SCHEMA_DIR / "checksums.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        on_disk = {p.name for p in SCHEMA_DIR.glob("*.sql")}
        assert set(manifest["files"].keys()) == on_disk

    def test_historical_fixture_manifest_covers_fixture(self) -> None:
        assert FIXTURE_CHECKSUMS.exists()
        manifest = json.loads(FIXTURE_CHECKSUMS.read_text(encoding="utf-8"))
        assert FIXTURE_SQL.name in manifest["files"]
        assert manifest["files"][FIXTURE_SQL.name] == _hash_file(FIXTURE_SQL)

    def test_migration_versions_have_no_gaps_or_duplicates(self) -> None:
        versions: list[int] = []
        for path in sorted(SCHEMA_DIR.glob("*.sql")):
            try:
                version = int(path.stem.split("_")[0])
            except (ValueError, IndexError):
                continue
            versions.append(version)
        assert versions == sorted(versions)
        assert len(versions) == len(set(versions))
        assert versions == list(range(1, len(versions) + 1))
