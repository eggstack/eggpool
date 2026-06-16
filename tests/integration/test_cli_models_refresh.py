"""Phase 18 integration tests for the ``models refresh`` CLI command.

Verifies that the standalone catalog refresh command produces the
same persisted account/model relationships as the application lifespan:

- File-backed database path;
- Two configured accounts with synthetic env keys;
- Upstream model catalog is mocked via ``respx``;
- After ``models refresh``, the database contains both accounts,
  models, and ``account_models`` relationships;
- With ``startup_refresh = false`` the application can still
  route cached models (no live upstream needed);
- Disabled accounts are persisted as disabled and do not become
  eligible for routing.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio
import respx
from click.testing import CliRunner

from go_aggregator.app import create_app
from go_aggregator.cli import cli
from go_aggregator.db.connection import Database

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI

UPSTREAM_BASE = "https://test-upstream.example.com"
TEST_KEY_ENV = "OPENCODE_TEST_KEY"
TEST_KEY_VALUE = "synthetic-cli-refresh-key"


def _build_toml_config(
    db_path: str,
    *,
    startup_refresh: bool = True,
) -> str:
    """Build a TOML configuration string for CLI tests."""
    return f"""
[server]
api_key_env = "{TEST_KEY_ENV}"
host = "127.0.0.1"
port = 0
log_level = "INFO"

[upstream]
base_url = "{UPSTREAM_BASE}"

[database]
path = "{db_path}"
wal = true
synchronous = "NORMAL"

[models]
refresh_interval_s = 0
expose_mode = "union"
startup_refresh = {str(startup_refresh).lower()}
stale_after_s = 7200
allow_stale_catalog = true

[routing]
strategy = "quota_fair"
max_retries_before_stream = 3
quota_exhausted_cooldown_seconds = 300

[limits]
five_hour_microdollars = 12000000
weekly_microdollars = 30000000
monthly_microdollars = 60000000

[dashboard]
enabled = false
public = false
retain_request_stats_days = 30
store_request_content = false
refresh_interval_s = 60

[security]
allowed_hosts = []
cors_origins = []
trust_proxy_headers = false
redact_headers = ["authorization", "x-api-key"]
persist_redacted_error_detail = false

[[accounts]]
name = "acct-alpha"
api_key_env = "{TEST_KEY_ENV}"
enabled = true
weight = 1.0

[[accounts]]
name = "acct-beta"
api_key_env = "{TEST_KEY_ENV}"
enabled = true
weight = 2.0
"""


def _build_disabled_accounts_toml(db_path: str) -> str:
    """TOML config with one enabled and one disabled account."""
    return f"""
[server]
api_key_env = "{TEST_KEY_ENV}"
host = "127.0.0.1"
port = 0
log_level = "INFO"

[upstream]
base_url = "{UPSTREAM_BASE}"

[database]
path = "{db_path}"
wal = true
synchronous = "NORMAL"

[models]
refresh_interval_s = 0
expose_mode = "union"
startup_refresh = true
stale_after_s = 7200
allow_stale_catalog = true

[routing]
strategy = "quota_fair"
max_retries_before_stream = 3
quota_exhausted_cooldown_seconds = 300

[limits]
five_hour_microdollars = 12000000
weekly_microdollars = 30000000
monthly_microdollars = 60000000

[dashboard]
enabled = false

[security]
persist_redacted_error_detail = false

[[accounts]]
name = "acct-active"
api_key_env = "{TEST_KEY_ENV}"
enabled = true
weight = 1.0

[[accounts]]
name = "acct-disabled"
api_key_env = "{TEST_KEY_ENV}"
enabled = false
weight = 1.0
"""


@pytest.fixture
def cli_api_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Set the synthetic API key environment variable."""
    monkeypatch.setenv(TEST_KEY_ENV, TEST_KEY_VALUE)
    return TEST_KEY_ENV


class TestModelsRefreshFileBacked:
    """``models refresh`` on a fresh file-backed database."""

    def test_refresh_creates_accounts_and_models(
        self,
        tmp_path,
        cli_api_key: str,
    ) -> None:
        """Refresh persists both configured accounts, models, and
        ``account_models`` rows that match the upstream mock."""
        db_path = str(tmp_path / "cli_refresh.sqlite3")
        config_path = tmp_path / "config.toml"
        config_path.write_text(_build_toml_config(db_path), encoding="utf-8")

        runner = CliRunner()
        with respx.mock:
            respx.get(f"{UPSTREAM_BASE}/models").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "object": "list",
                        "data": [
                            {
                                "id": "gpt-4",
                                "object": "model",
                                "owned_by": "openai",
                            },
                            {
                                "id": "claude-3",
                                "object": "model",
                                "owned_by": "anthropic",
                            },
                        ],
                    },
                )
            )
            result = runner.invoke(
                cli,
                ["--config", str(config_path), "models", "refresh"],
            )

        assert result.exit_code == 0, (
            f"CLI failed: exit={result.exit_code} stdout={result.stdout} "
            f"stderr={getattr(result, 'stderr', '')}"
        )
        assert "Refreshed catalog" in result.stdout

        # Reopen database and verify rows.
        async def _inspect() -> None:
            db = Database(path=db_path)
            await db.connect()
            try:
                acct_rows = await db.fetch_all(
                    "SELECT name, enabled, weight FROM accounts ORDER BY name"
                )
                names = [row["name"] for row in acct_rows]
                assert names == ["acct-alpha", "acct-beta"]
                weights = {row["name"]: row["weight"] for row in acct_rows}
                assert weights["acct-alpha"] == 1.0
                assert weights["acct-beta"] == 2.0

                model_rows = await db.fetch_all(
                    "SELECT model_id, protocol FROM models ORDER BY model_id"
                )
                model_ids = {row["model_id"]: row["protocol"] for row in model_rows}
                assert "gpt-4" in model_ids
                assert "claude-3" in model_ids

                am_rows = await db.fetch_all(
                    "SELECT a.name AS account_name, m.model_id "
                    "FROM account_models am "
                    "JOIN accounts a ON a.id = am.account_id "
                    "JOIN models m ON m.model_id = am.model_id "
                    "WHERE am.enabled = 1 "
                    "ORDER BY a.name, m.model_id"
                )
                pairs = [(row["account_name"], row["model_id"]) for row in am_rows]
                # Both accounts should support both models.
                expected_pairs = {
                    ("acct-alpha", "claude-3"),
                    ("acct-alpha", "gpt-4"),
                    ("acct-beta", "claude-3"),
                    ("acct-beta", "gpt-4"),
                }
                assert set(pairs) == expected_pairs
            finally:
                await db.disconnect()

        import asyncio

        asyncio.run(_inspect())

    def test_refresh_on_nonexistent_db_creates_file(
        self,
        tmp_path,
        cli_api_key: str,
    ) -> None:
        """Refresh on a missing file-backed database creates the file."""
        db_path = str(tmp_path / "fresh.sqlite3")
        config_path = tmp_path / "config.toml"
        config_path.write_text(_build_toml_config(db_path), encoding="utf-8")

        assert not os.path.exists(db_path)

        runner = CliRunner()
        with respx.mock:
            respx.get(f"{UPSTREAM_BASE}/models").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "object": "list",
                        "data": [
                            {
                                "id": "gpt-4",
                                "object": "model",
                                "owned_by": "openai",
                            },
                        ],
                    },
                )
            )
            result = runner.invoke(
                cli,
                ["--config", str(config_path), "models", "refresh"],
            )

        assert result.exit_code == 0
        assert os.path.exists(db_path)


class TestModelsRefreshCachedRoutable:
    """With ``startup_refresh = false`` cached models stay routable."""

    @pytest_asyncio.fixture()
    async def cached_app(
        self,
        tmp_path,
        cli_api_key: str,
    ) -> AsyncGenerator[FastAPI]:
        """Build a database with cached models, then start the app.

        The catalog has been refreshed and persisted. The app is
        started with ``startup_refresh = false`` so the application
        does not contact upstream at startup.
        """
        db_path = str(tmp_path / "cached.sqlite3")
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            _build_toml_config(db_path, startup_refresh=True),
            encoding="utf-8",
        )

        # Pre-populate the database with accounts and a cached model
        # by running the same operations the CLI ``models refresh``
        # command performs. We call the command body via Click's
        # standalone ``main`` invocation, but since the test runs in
        # an event loop we cannot use ``asyncio.run``. The CLI command
        # body is just a thin wrapper, so we replicate the steps in
        # the same event loop.
        from go_aggregator.accounts.registry import (
            AccountRegistry,
            account_config_rows,
        )
        from go_aggregator.catalog.service import CatalogService
        from go_aggregator.db.migrations import MigrationRunner
        from go_aggregator.db.repositories import AccountRepository
        from go_aggregator.models.config import AppConfig

        async def _populate() -> None:
            config = AppConfig.from_toml(str(config_path))
            db = Database(
                path=config.database.path,
                busy_timeout_ms=config.database.busy_timeout_ms,
                wal=config.database.wal,
                synchronous=config.database.synchronous,
            )
            await db.connect()
            try:
                runner = MigrationRunner(db)
                await runner.run()
                account_repo = AccountRepository(db)
                await account_repo.sync_from_config(account_config_rows(config), db)
                registry = AccountRegistry(config)
                async with httpx.AsyncClient(
                    base_url=config.upstream.base_url
                ) as client:
                    catalog = CatalogService(config, registry, db, client)
                    await catalog.refresh()
            finally:
                await db.disconnect()

        with respx.mock:
            respx.get(f"{UPSTREAM_BASE}/models").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "object": "list",
                        "data": [
                            {
                                "id": "gpt-4",
                                "object": "model",
                                "owned_by": "openai",
                            },
                        ],
                    },
                )
            )
            await _populate()

        # Now construct a second app that disables startup refresh
        # and verify it can still find the cached model.
        cached_config_path = tmp_path / "config_no_refresh.toml"
        cached_config_path.write_text(
            _build_toml_config(db_path, startup_refresh=False),
            encoding="utf-8",
        )

        from go_aggregator.models.config import AppConfig as _AppConfig

        cached_config = _AppConfig.from_toml(str(cached_config_path))
        application = create_app(cached_config)

        # The app lifespan would normally call upstream at startup; we
        # block that with respx while loading the lifespan so the
        # background catalog refresh task cannot reach the network.
        with respx.mock:
            respx.get(f"{UPSTREAM_BASE}/models").mock(
                return_value=httpx.Response(
                    200,
                    json={"object": "list", "data": []},
                )
            )
            async with application.router.lifespan_context(application):
                yield application

    @pytest.mark.asyncio
    async def test_cached_models_routable_after_startup(
        self,
        cached_app: FastAPI,
    ) -> None:
        """``/v1/models`` returns the cached model when startup_refresh is off."""
        transport = httpx.ASGITransport(app=cached_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.get(
                "/v1/models",
                headers={"Authorization": f"Bearer {TEST_KEY_VALUE}"},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        ids = {m["id"] for m in body["data"]}
        assert "gpt-4" in ids

    @pytest.mark.asyncio
    async def test_cached_accounts_loaded_at_startup(
        self,
        cached_app: FastAPI,
    ) -> None:
        """The persisted accounts are visible to the running app."""
        db: Database = cached_app.state.db
        rows = await db.fetch_all("SELECT name, enabled FROM accounts ORDER BY name")
        names = [row["name"] for row in rows]
        assert names == ["acct-alpha", "acct-beta"]


class TestModelsRefreshDisabledAccount:
    """Disabled accounts persist as disabled and are not eligible."""

    def test_disabled_account_persisted_not_eligible(
        self,
        tmp_path,
        cli_api_key: str,
    ) -> None:
        """Disabled accounts are stored with enabled=0 and do not
        receive any model relationships."""
        db_path = str(tmp_path / "disabled.sqlite3")
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            _build_disabled_accounts_toml(db_path),
            encoding="utf-8",
        )

        runner = CliRunner()
        with respx.mock:
            respx.get(f"{UPSTREAM_BASE}/models").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "object": "list",
                        "data": [
                            {
                                "id": "gpt-4",
                                "object": "model",
                                "owned_by": "openai",
                            },
                        ],
                    },
                )
            )
            result = runner.invoke(
                cli,
                ["--config", str(config_path), "models", "refresh"],
            )
        assert result.exit_code == 0, result.stdout

        import asyncio

        async def _inspect() -> None:
            db = Database(path=db_path)
            await db.connect()
            try:
                rows = await db.fetch_all(
                    "SELECT name, enabled FROM accounts ORDER BY name"
                )
                by_name = {row["name"]: bool(row["enabled"]) for row in rows}
                assert by_name["acct-active"] is True
                assert by_name["acct-disabled"] is False

                # Models still get persisted.
                model_rows = await db.fetch_all("SELECT model_id FROM models")
                assert {r["model_id"] for r in model_rows} == {"gpt-4"}

                # The disabled account's account_models row exists but
                # is marked enabled=0; the enabled account's row is
                # enabled=1. This proves the disabled account cannot
                # make a model eligible for routing.
                am_rows = await db.fetch_all(
                    "SELECT a.name AS account_name, am.enabled "
                    "FROM account_models am "
                    "JOIN accounts a ON a.id = am.account_id "
                    "ORDER BY a.name"
                )
                am_by_name = {
                    row["account_name"]: bool(row["enabled"]) for row in am_rows
                }
                assert am_by_name.get("acct-active") is True
                assert am_by_name.get("acct-disabled") is False
            finally:
                await db.disconnect()

        asyncio.run(_inspect())
