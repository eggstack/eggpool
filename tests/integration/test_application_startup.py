"""Phase 17 application startup integration test.

Verifies that a fresh file-backed database can complete the full
application lifespan (migrations, account sync, crash recovery, catalog
refresh, and shutdown) without leaving an implicit transaction open
or corrupting in-memory state.
"""

from __future__ import annotations

import os

import httpx
import pytest
import respx

from eggpool.app import create_app
from eggpool.db.connection import Database
from eggpool.errors import DatabaseError
from eggpool.models.config import AppConfig

UPSTREAM_BASE = "https://test-upstream.example.com"
TEST_KEY = "test-startup-key-value"


@pytest.fixture
def api_key_env(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("OPENCODE_TEST_KEY", TEST_KEY)
    return "OPENCODE_TEST_KEY"


@pytest.fixture
def config_file_db(
    tmp_path,
    api_key_env: str,
) -> AppConfig:
    db_path = tmp_path / "app_startup.sqlite3"
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": api_key_env,
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": str(db_path), "wal": True},
            "upstream": {"base_url": UPSTREAM_BASE},
            "models": {
                "startup_refresh": True,
                "refresh_interval_s": 0,
            },
            "accounts": [
                {"name": "acct-alpha", "api_key_env": api_key_env},
                {"name": "acct-beta", "api_key_env": api_key_env},
            ],
            "dashboard": {"enabled": False},
        }
    )


class TestApplicationStartup:
    """Verify a complete fresh startup lifecycle."""

    @pytest.mark.asyncio
    async def test_full_lifespan_with_file_backed_db(
        self,
        config_file_db: AppConfig,
        tmp_path,
    ) -> None:
        """Startup creates the file, syncs accounts, recovers, and shuts down."""
        app = create_app(config_file_db)
        db_path = config_file_db.database.path

        assert not os.path.exists(db_path), (
            "File-backed DB should not exist before startup."
        )

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

            async with app.router.lifespan_context(app):
                db: Database = app.state.db
                assert db is not None
                assert db._conn is not None  # noqa: SLF001
                stats_db: Database = app.state.stats_db
                assert stats_db is db

                rows = await db.fetch_all("SELECT name FROM accounts ORDER BY name")
                names = [row["name"] for row in rows]
                assert names == ["acct-alpha", "acct-beta"]

                accounts_active = await db.fetch_all(
                    "SELECT COUNT(*) AS c FROM accounts WHERE enabled = 1"
                )
                assert accounts_active[0]["c"] == 2

                assert os.path.exists(db_path), (
                    "Database file should be created during startup."
                )

                crash_recovery_invocation_ok = await _check_implicit_tx(db)
                assert crash_recovery_invocation_ok, (
                    "Crash recovery must be able to start a transaction "
                    "after account sync without 'cannot start a transaction "
                    "within a transaction'."
                )

                conn = db._conn  # noqa: SLF001
                assert conn is not None

            assert db._conn is None, (  # noqa: SLF001
                "Connection should be released after shutdown."
            )
            assert stats_db._conn is None  # noqa: SLF001

        # Reopen the database; rows should still be there.
        db2 = Database(path=db_path)
        await db2.connect()
        try:
            rows = await db2.fetch_all("SELECT name FROM accounts ORDER BY name")
            names = [row["name"] for row in rows]
            assert names == ["acct-alpha", "acct-beta"]
        finally:
            await db2.disconnect()

    @pytest.mark.asyncio
    async def test_worker_threads_two_opens_separate_stats_connection(
        self,
        config_file_db: AppConfig,
    ) -> None:
        """``database.worker_threads = 2`` opts into a read-only stats thread."""
        config = config_file_db.model_copy(deep=True)
        config.database.worker_threads = 2
        app = create_app(config)

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

            async with app.router.lifespan_context(app):
                db: Database = app.state.db
                stats_db: Database = app.state.stats_db
                assert stats_db is not db
                assert stats_db.read_only is True
                assert stats_db._conn is not None  # noqa: SLF001

            assert db._conn is None  # noqa: SLF001
            assert stats_db._conn is None  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_restart_succeeds_after_full_lifecycle(
        self,
        config_file_db: AppConfig,
    ) -> None:
        """A second startup succeeds without nested-transaction errors."""
        app1 = create_app(config_file_db)
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
            async with app1.router.lifespan_context(app1):
                pass

        # Second startup must succeed and find the existing accounts.
        app2 = create_app(config_file_db)
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
            async with app2.router.lifespan_context(app2):
                db: Database = app2.state.db
                rows = await db.fetch_all("SELECT name FROM accounts ORDER BY name")
                names = [row["name"] for row in rows]
                assert names == ["acct-alpha", "acct-beta"]


async def _check_implicit_tx(db: Database) -> bool:
    """Verify a new transaction can be started immediately.

    If an earlier step left an implicit transaction open, this raises
    ``cannot start a transaction within a transaction``. We catch and
    return False in that case so the test reports a clean failure.
    """
    try:
        async with db.transaction():
            await db.fetch_one("SELECT 1 AS one")
        return True
    except DatabaseError:
        return False
    except Exception:
        return False


class TestApplicationStartupNoAuth:
    """Startup with auth disabled succeeds without an api_key_env."""

    @pytest.mark.asyncio
    async def test_no_auth_startup(self, tmp_path) -> None:
        db_path = tmp_path / "no_auth.sqlite3"
        config = AppConfig.from_dict(
            {
                "server": {
                    "api_key_env": "",
                    "host": "127.0.0.1",
                    "port": 0,
                },
                "database": {"path": str(db_path), "wal": True},
                "upstream": {"base_url": UPSTREAM_BASE},
                "models": {
                    "startup_refresh": False,
                    "refresh_interval_s": 0,
                },
                "accounts": [],
                "dashboard": {"enabled": False},
            }
        )
        app = create_app(config)
        async with app.router.lifespan_context(app):
            db: Database = app.state.db
            rows = await db.fetch_all(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            assert len(rows) > 0, "Tables should exist after migrations."


class TestApplicationStartupRequiresAuth:
    """Startup fails fast if auth is enabled but the API key is invalid."""

    @pytest.mark.asyncio
    async def test_placeholder_api_key_raises(self, tmp_path) -> None:
        """Startup rejects placeholder API key values."""
        from eggpool.errors import AggregatorError

        config = AppConfig.from_dict(
            {
                "server": {
                    "api_key": "your-api-key-here",
                    "host": "127.0.0.1",
                    "port": 0,
                },
                "database": {"path": str(tmp_path / "auth.sqlite3")},
                "upstream": {"base_url": UPSTREAM_BASE},
                "models": {
                    "startup_refresh": False,
                    "refresh_interval_s": 0,
                },
                "accounts": [],
                "dashboard": {"enabled": False},
            }
        )

        app = create_app(config)
        with pytest.raises(Exception) as excinfo:
            async with app.router.lifespan_context(app):
                pass
        assert "placeholder" in str(excinfo.value).lower() or isinstance(
            excinfo.value, AggregatorError
        )


@pytest.mark.asyncio
async def test_startup_failure_closes_all_initialized_resources(
    tmp_path,
) -> None:
    """Failures before lifespan yield still close databases and clients.

    The PID file is owned by the ``eggpool serve`` supervisor and is no
    longer created or removed by lifespan, so this test no longer
    asserts anything about the PID file.
    """
    from eggpool.errors import CatalogUnavailableError

    config = AppConfig.from_dict(
        {
            "server": {"api_key_env": "", "host": "127.0.0.1", "port": 0},
            "database": {"path": str(tmp_path / "startup-failure.sqlite3")},
            "models": {
                "startup_refresh": False,
                "refresh_interval_s": 0,
                "allow_stale_catalog": False,
            },
            "accounts": [],
            "dashboard": {"enabled": False},
        }
    )
    app = create_app(config)

    with pytest.raises(CatalogUnavailableError):
        async with app.router.lifespan_context(app):
            pass

    assert app.state.db._conn is None  # noqa: SLF001
    assert app.state.stats_db._conn is None  # noqa: SLF001
    assert all(client.is_closed for client in app.state.client_pool._clients.values())  # noqa: SLF001
