"""Phase 9 integration tests for provider-aware CLI commands.

Tests that the CLI correctly:
- Uses ProviderClientPool for ``models refresh`` with multiple providers;
- Displays the provider column in ``accounts status`` output;
- Handles per-provider client configuration.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from click.testing import CliRunner

from go_aggregator.cli import cli
from go_aggregator.db.connection import Database

UPSTREAM_BASE_OPENCODE = "https://opencode-upstream.example.com"
UPSTREAM_BASE_ANTHROPIC = "https://anthropic-upstream.example.com"
TEST_KEY_ENV_1 = "TEST_PROVIDER_KEY_1"
TEST_KEY_ENV_2 = "TEST_PROVIDER_KEY_2"
TEST_KEY_ENV_3 = "TEST_PROVIDER_KEY_3"


def _build_multi_provider_toml(db_path: str) -> str:
    """TOML config with two providers and accounts."""
    return f"""
[server]
api_key_env = "TEST_GLOBAL_KEY"
host = "127.0.0.1"
port = 0
log_level = "INFO"

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
public = false
retain_request_stats_days = 30
store_request_content = false
refresh_interval_s = 60

[security]
allowed_hosts = []
cors_origins = []
redact_headers = ["authorization", "x-api-key"]
persist_redacted_error_detail = false

[providers.opencode-go]
id = "opencode-go"
base_url = "{UPSTREAM_BASE_OPENCODE}"
protocols = ["openai"]
models_method = "GET"
models_path = "/models"

[[providers.opencode-go.accounts]]
name = "acct-oc-1"
api_key_env = "{TEST_KEY_ENV_1}"
enabled = true
weight = 1.0

[providers.anthropic-proxy]
id = "anthropic-proxy"
base_url = "{UPSTREAM_BASE_ANTHROPIC}"
protocols = ["anthropic"]
models_method = "GET"
models_path = "/v1/models"

[[providers.anthropic-proxy.accounts]]
name = "acct-anth-1"
api_key_env = "{TEST_KEY_ENV_2}"
enabled = true
weight = 1.0
"""


def _build_single_provider_toml(db_path: str) -> str:
    """TOML config with one provider and accounts (legacy flat style)."""
    return f"""
[server]
api_key_env = "TEST_GLOBAL_KEY"
host = "127.0.0.1"
port = 0
log_level = "INFO"

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
name = "single-acct-1"
api_key_env = "{TEST_KEY_ENV_1}"
enabled = true
weight = 1.0

[[accounts]]
name = "single-acct-2"
api_key_env = "{TEST_KEY_ENV_2}"
enabled = true
weight = 2.0
"""


@pytest.fixture
def provider_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set synthetic API keys for provider accounts."""
    monkeypatch.setenv(TEST_KEY_ENV_1, "synthetic-key-1")
    monkeypatch.setenv(TEST_KEY_ENV_2, "synthetic-key-2")
    monkeypatch.setenv(TEST_KEY_ENV_3, "synthetic-key-3")
    monkeypatch.setenv("TEST_GLOBAL_KEY", "global-test-key")


class TestModelsRefreshMultiProvider:
    """``models refresh`` with multiple providers creates per-provider clients."""

    def test_refresh_with_multiple_providers(
        self,
        tmp_path,
        provider_api_keys,
    ) -> None:
        """Refresh with two providers fetches from each provider's upstream."""
        db_path = str(tmp_path / "multi_provider.sqlite3")
        config_path = tmp_path / "config.toml"
        config_path.write_text(_build_multi_provider_toml(db_path), encoding="utf-8")

        runner = CliRunner()
        with respx.mock:
            # Mock the opencode-go provider's /models endpoint
            respx.get(f"{UPSTREAM_BASE_OPENCODE}/models").mock(
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
            # Mock the anthropic-proxy provider's /v1/models endpoint
            respx.get(f"{UPSTREAM_BASE_ANTHROPIC}/v1/models").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "object": "list",
                        "data": [
                            {
                                "id": "claude-3-opus",
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

        # Verify both providers' models were fetched
        async def _inspect() -> None:
            db = Database(path=db_path)
            await db.connect()
            try:
                model_rows = await db.fetch_all(
                    "SELECT model_id FROM models ORDER BY model_id"
                )
                model_ids = {row["model_id"] for row in model_rows}
                assert "gpt-4" in model_ids
                assert "claude-3-opus" in model_ids
            finally:
                await db.disconnect()

        import asyncio

        asyncio.run(_inspect())

    def test_refresh_uses_provider_specific_paths(
        self,
        tmp_path,
        provider_api_keys,
    ) -> None:
        """Each provider's models_path is used for fetching."""
        db_path = str(tmp_path / "provider_paths.sqlite3")
        config_path = tmp_path / "config.toml"
        config_path.write_text(_build_multi_provider_toml(db_path), encoding="utf-8")

        runner = CliRunner()
        with respx.mock:
            # opencode-go uses /models
            respx.get(f"{UPSTREAM_BASE_OPENCODE}/models").mock(
                return_value=httpx.Response(
                    200,
                    json={"object": "list", "data": [{"id": "gpt-4"}]},
                )
            )
            # anthropic-proxy uses /v1/models
            respx.get(f"{UPSTREAM_BASE_ANTHROPIC}/v1/models").mock(
                return_value=httpx.Response(
                    200,
                    json={"object": "list", "data": [{"id": "claude-3"}]},
                )
            )
            result = runner.invoke(
                cli,
                ["--config", str(config_path), "models", "refresh"],
            )

        assert result.exit_code == 0


class TestAccountsStatusProviderColumn:
    """``accounts status`` shows provider information."""

    def test_accounts_status_displays_provider(
        self,
        tmp_path,
        provider_api_keys,
    ) -> None:
        """Status output includes provider column for each account."""
        db_path = str(tmp_path / "status_provider.sqlite3")
        config_path = tmp_path / "config.toml"
        config_path.write_text(_build_multi_provider_toml(db_path), encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "accounts", "status"],
        )

        assert result.exit_code == 0
        assert "provider=opencode-go" in result.stdout
        assert "provider=anthropic-proxy" in result.stdout
        assert "acct-oc-1" in result.stdout
        assert "acct-anth-1" in result.stdout

    def test_accounts_status_single_provider(
        self,
        tmp_path,
        provider_api_keys,
    ) -> None:
        """Status output for legacy flat accounts shows default provider."""
        db_path = str(tmp_path / "status_single.sqlite3")
        config_path = tmp_path / "config.toml"
        config_path.write_text(_build_single_provider_toml(db_path), encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "accounts", "status"],
        )

        assert result.exit_code == 0
        # Legacy flat accounts should be normalized to default provider
        assert "provider=opencode-go" in result.stdout
        assert "single-acct-1" in result.stdout
        assert "single-acct-2" in result.stdout

    def test_accounts_status_api_key_set(
        self,
        tmp_path,
        provider_api_keys,
    ) -> None:
        """Status output shows api_key_env set status."""
        db_path = str(tmp_path / "status_key.sqlite3")
        config_path = tmp_path / "config.toml"
        config_path.write_text(_build_multi_provider_toml(db_path), encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "accounts", "status"],
        )

        assert result.exit_code == 0
        assert "(set=yes)" in result.stdout

    def test_accounts_status_empty(self, tmp_path) -> None:
        """Status output with no accounts configured."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            """
[server]
api_key_env = "TEST_GLOBAL_KEY"

[database]
path = ":memory:"

[models]
refresh_interval_s = 0
expose_mode = "union"
startup_refresh = false

[routing]
strategy = "quota_fair"

[limits]
five_hour_microdollars = 12000000
weekly_microdollars = 30000000
monthly_microdollars = 60000000

[dashboard]
enabled = false

[security]
persist_redacted_error_detail = false
""",
            encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "accounts", "status"],
        )

        assert result.exit_code == 0
        assert "No accounts configured." in result.stdout
