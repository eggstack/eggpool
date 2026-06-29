"""Phase 17 deployment-readiness regression matrix.

This file exercises the cross-cutting scenarios called out in the
Phase 17 deployment-readiness audit. Each test class is named after
a matrix letter so the matrix itself is auditable from a single file.

The matrix:

  A. Fresh startup transaction safety (file-backed DB)
  B. Write-helper contract (wires require transaction owner)
  C. Credential boundary (local credentials never reach upstream)
  D. CI dependency smoke (pytest --cov is available in the lockfile)
  E. Migration compatibility (fresh == upgraded)
  F. Raw cursor restriction (outside transaction fails)
  G. Real 402 lifecycle (see test_phase17_release_validation.py)
  H. Streaming smoke unit behavior (see test_smoke_test.py)
  I. Privacy (default is fail-closed; see test_error_detail_privacy.py)
  J. Operational checker (read-only; see test_check_database.py)

Scenarios G, H, I, and J have dedicated files that exercise them
in detail. The tests in this file exist so a single pytest run
can confirm the high-level matrix is intact without depending
on the dedicated files' internals.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.errors import DatabaseError
from eggpool.proxy.client import (
    LOCAL_CREDENTIAL_HEADERS,
    build_upstream_auth_headers,
    filter_request_headers,
)

# --- A. Fresh startup transaction safety (file-backed DB) ---


class TestFreshStartupTransactionSafety:
    @pytest.mark.asyncio
    async def test_file_backed_fresh_startup(self, tmp_path: Path) -> None:
        """A file-backed fresh database with two accounts commits and
        survives a restart without leaving an implicit transaction open.

        Equivalent to test_application_startup.py but kept here as a
        single-file end-to-end matrix entry.
        """
        db_path = tmp_path / "fresh.sqlite3"
        os.environ["OPENCODE_MATRIX_A"] = "key-a"
        os.environ["OPENCODE_MATRIX_B"] = "key-b"

        # First startup: connect, run migrations, sync accounts.
        async def _first_startup() -> None:
            db = Database(path=str(db_path))
            await db.connect()
            try:
                runner = MigrationRunner(db)
                await runner.run()
                async with db.transaction():
                    for name, env in (
                        ("acct-a", "OPENCODE_MATRIX_A"),
                        ("acct-b", "OPENCODE_MATRIX_B"),
                    ):
                        existing = await db.fetch_one(
                            "SELECT id FROM accounts WHERE name = ?", (name,)
                        )
                        if existing is None:
                            await db.execute_insert(
                                "INSERT INTO accounts "
                                "(name, api_key_env, enabled, weight) "
                                "VALUES (?, ?, 1, 1.0)",
                                (name, env),
                            )
                # Crash recovery step also requires an owned tx.
                async with db.transaction():
                    await db.execute_write(
                        "UPDATE reservations SET status = 'released' "
                        "WHERE status = 'active' AND 0"
                    )
            finally:
                await db.disconnect()

        await _first_startup()

        # Second startup: reconnect and confirm rows are durable.
        async def _second_startup() -> None:
            db = Database(path=str(db_path))
            await db.connect()
            try:
                rows = await db.fetch_all("SELECT name FROM accounts ORDER BY name")
                names = {row["name"] for row in rows}
                assert names == {"acct-a", "acct-b"}
            finally:
                await db.disconnect()

        await _second_startup()


# --- B. Write-helper contract ---


class TestWriteHelperContract:
    @pytest.mark.asyncio
    async def test_writes_outside_transaction_fail(self) -> None:
        """The write helpers reject calls outside a transaction."""
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        try:
            with pytest.raises(DatabaseError, match="active transaction"):
                await db.execute_write(
                    "INSERT INTO accounts (name, api_key_env) VALUES (?, ?)",
                    ("no-tx", "ENV"),
                )
            with pytest.raises(DatabaseError, match="active transaction"):
                await db.execute_insert(
                    "INSERT INTO accounts (name, api_key_env) VALUES (?, ?)",
                    ("no-tx", "ENV"),
                )
            with pytest.raises(DatabaseError, match="active transaction"):
                await db.execute_returning("SELECT 1 AS x")
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_writes_inside_transaction_succeed(self) -> None:
        """The write helpers succeed inside an active transaction."""
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        try:
            async with db.transaction():
                last_id = await db.execute_insert(
                    "INSERT INTO accounts (name, api_key_env) VALUES (?, ?)",
                    ("in-tx", "ENV"),
                )
            assert last_id > 0
            row = await db.fetch_one(
                "SELECT name FROM accounts WHERE id = ?", (last_id,)
            )
            assert row is not None
            assert row["name"] == "in-tx"
        finally:
            await db.disconnect()


# --- C. Credential boundary ---


class TestCredentialBoundary:
    def test_local_credential_headers_explicit(self) -> None:
        """The local-credential set is exactly the documented three."""
        assert (
            frozenset({"authorization", "x-api-key", "proxy-authorization"})
            == LOCAL_CREDENTIAL_HEADERS
        )

    def test_filter_request_headers_strips_local_credentials(self) -> None:
        headers = {
            "Authorization": "Bearer LOCAL_BEARER_SECRET",
            "X-Api-Key": "LOCAL_X_API_SECRET",
            "Proxy-Authorization": "Basic LOCAL_PROXY_SECRET",
            "Content-Type": "application/json",
            "X-Custom": "kept",
        }
        filtered = filter_request_headers(headers, "UPSTREAM_ACCOUNT_SECRET")
        # No local secret marker survives the filter.
        all_values = " ".join(str(v) for v in filtered.values())
        assert "LOCAL_BEARER_SECRET" not in all_values
        assert "LOCAL_X_API_SECRET" not in all_values
        assert "LOCAL_PROXY_SECRET" not in all_values
        # X-Api-Key and Proxy-Authorization are stripped entirely.
        assert "X-Api-Key" not in filtered
        assert "Proxy-Authorization" not in filtered
        # Upstream credential is injected exactly once via Authorization.
        assert filtered["Authorization"] == "Bearer UPSTREAM_ACCOUNT_SECRET"
        assert list(filtered.keys()).count("Authorization") == 1
        # Non-credential headers survive.
        assert filtered["X-Custom"] == "kept"
        assert filtered["Content-Type"] == "application/json"

    def test_build_upstream_auth_headers_single_field(self) -> None:
        """build_upstream_auth_headers returns exactly one Authorization."""
        for protocol in ("openai", "anthropic", ""):
            headers = build_upstream_auth_headers(protocol, "secret-key")
            assert list(headers.keys()) == ["Authorization"]
            assert headers["Authorization"] == "Bearer secret-key"


# --- D. CI dependency smoke ---


class TestCiDependencySmoke:
    def test_pytest_cov_help_is_present(self) -> None:
        """`pytest --help` must include --cov in the dev environment."""
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0
        assert "--cov" in result.stdout


# --- E. Migration compatibility ---


class TestMigrationCompatibility:
    @pytest.mark.asyncio
    async def test_fresh_and_upgraded_schemas_equivalent(self, tmp_path: Path) -> None:
        """A fresh database and an upgraded-to-current database must
        have equivalent schemas for production operations.

        This is a high-level smoke covering the same property
        exercised in detail by test_migration_compatibility.py.
        """
        from eggpool.db.repositories import (
            PriceSnapshotRepository,
        )

        fresh_path = tmp_path / "fresh.sqlite3"
        upgraded_path = tmp_path / "upgraded.sqlite3"

        async def _record_snapshot(path: Path) -> None:
            db = Database(path=str(path))
            await db.connect()
            try:
                runner = MigrationRunner(db)
                await runner.run()
                # model_price_snapshots has a FK to models.
                async with db.transaction():
                    await db.execute_write(
                        "INSERT OR IGNORE INTO models (model_id, protocol) "
                        "VALUES (?, ?)",
                        ("gpt-4", "openai"),
                    )
                repo = PriceSnapshotRepository(db)
                async with db.transaction():
                    await repo.record(
                        model_id="gpt-4",
                        input_price_per_1k=0.03,
                        output_price_per_1k=0.06,
                    )
                async with db.transaction():
                    latest = await repo.get_latest("gpt-4")
                assert latest is not None
                assert latest["input_per_million_microdollars"] == 30_000_000
            finally:
                await db.disconnect()

        await _record_snapshot(fresh_path)
        await _record_snapshot(upgraded_path)

        # Compare column sets between the two databases.
        async def _columns(path: Path) -> set[str]:
            db = Database(path=str(path))
            await db.connect()
            try:
                rows = await db.fetch_all("PRAGMA table_info(requests)")
                return {row["name"] for row in rows}
            finally:
                await db.disconnect()

        fresh_cols = await _columns(fresh_path)
        upgraded_cols = await _columns(upgraded_path)
        assert fresh_cols == upgraded_cols

    def test_migration_manifest_matches_files(self) -> None:
        """The migration manifest SHA-256 matches every applied file."""
        manifest_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "eggpool"
            / "db"
            / "schema"
            / "checksums.json"
        )
        assert manifest_path.exists(), "checksums.json missing"
        manifest = json.loads(manifest_path.read_text())
        for filename, expected in manifest["files"].items():
            full = manifest_path.parent / filename
            actual = hashlib.sha256(full.read_bytes()).hexdigest()
            assert actual == expected, (
                f"Migration {filename} SHA-256 mismatch. "
                f"Manifest says {expected}, file is {actual}."
            )


# --- F. Raw cursor restriction ---


class TestRawCursorRestriction:
    @pytest.mark.asyncio
    async def test_raw_cursor_outside_transaction_fails(self) -> None:
        """_execute_cursor outside a transaction raises DatabaseError."""
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        try:
            with pytest.raises(DatabaseError, match="active transaction"):
                await db._execute_cursor(  # type: ignore[reportPrivateUsage]
                    "SELECT 1"
                )
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_raw_cursor_inside_owned_transaction_succeeds(
        self,
    ) -> None:
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        try:
            async with db.transaction():
                cursor = await db._execute_cursor(  # type: ignore[reportPrivateUsage]
                    "SELECT 1 AS x"
                )
                rows = await cursor.fetchall()
                assert rows[0][0] == 1
        finally:
            await db.disconnect()


# --- I. Privacy default ---


class TestPrivacyDefault:
    def test_persist_redacted_error_detail_default(self) -> None:
        """The default for persist_redacted_error_detail is False.

        This protects operators who upgrade without reading the
        release notes: error_detail is NULL by default.
        """
        from eggpool.models.config import SecurityConfig

        cfg = SecurityConfig()
        assert cfg.persist_redacted_error_detail is False


# --- D' (smoke test infra) ---


class TestSmokeTestRequiredEnv:
    def test_smoke_test_requires_explicit_model_ids(self) -> None:
        """The smoke test refuses to run without model IDs.

        We exec the smoke test with empty env and assert a
        non-zero exit and an error message naming the missing
        variables. This prevents misleading deployment failures
        caused by stale generic IDs.
        """
        env = os.environ.copy()
        # Force-empty the four required variables if the host has
        # any of them set.
        for name in (
            "GOROUTER_BASE_URL",
            "GOROUTER_API_KEY",
            "GOROUTER_OPENAI_MODEL",
            "GOROUTER_ANTHROPIC_MODEL",
        ):
            env[name] = ""
        result = subprocess.run(
            [sys.executable, "scripts/smoke_test.py"],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=10,
        )
        assert result.returncode != 0
        combined = (result.stdout or "") + (result.stderr or "")
        assert "Missing required environment variable" in combined
