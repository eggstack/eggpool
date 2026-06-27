"""End-to-end tests for API key flow and CLI config commands.

Verifies:
- API key written by connect flows through to upstream Authorization header
- set_config host/port updates config correctly and signals server
- newkey generates a key, saves it, and signals server reload
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio
import respx

from eggpool.accounts.registry import AccountRegistry
from eggpool.catalog.service import CatalogService
from eggpool.cli import _read_server_api_key, _update_server_config, cli
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
)
from eggpool.health.health_manager import HealthManager
from eggpool.models.config import AppConfig
from eggpool.request.coordinator import RequestCoordinator
from eggpool.routing.router import Router

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI

UPSTREAM_BASE = "https://echo.example.com"
UPSTREAM_KEY = "sk-real-provider-key-abc123"
LOCAL_KEY = "ep_local_server_key_xyz789"


def _build_config() -> AppConfig:
    """Build config simulating what connect writes to config.toml."""
    return AppConfig.from_dict(
        {
            "server": {
                "api_key": LOCAL_KEY,
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": UPSTREAM_BASE},
            "models": {
                "startup_refresh": False,
                "refresh_interval_s": 0,
            },
            "accounts": [
                {
                    "name": "test-acct",
                    "api_key": UPSTREAM_KEY,
                },
            ],
            "dashboard": {"enabled": False},
        }
    )


@pytest.fixture
def config() -> AppConfig:
    return _build_config()


@pytest_asyncio.fixture()
async def app(config: AppConfig) -> AsyncGenerator[FastAPI]:
    from eggpool.app import create_app

    application = create_app(config)

    db = Database(path=":memory:")
    await db.connect()
    application.state.db = db

    runner = MigrationRunner(db)
    await runner.run()

    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("test-acct", ""),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )

    httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(5.0, connect=5.0, read=5.0, write=5.0, pool=5.0),
        limits=httpx.Limits(
            max_connections=10,
            max_keepalive_connections=5,
            keepalive_expiry=30.0,
        ),
    )
    application.state.httpx_client = httpx_client

    registry = AccountRegistry(config)
    application.state.registry = registry

    catalog = CatalogService(config, registry, db, httpx_client)
    application.state.catalog = catalog

    router = Router(registry, catalog)
    application.state.router = router

    health_manager = HealthManager()
    application.state.health_manager = health_manager

    request_repo = RequestRepository(db)
    reservation_repo = ReservationRepository(db)
    attempt_repo = AttemptRepository(db)

    coordinator = RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=db,
        client_pool=httpx_client,
        request_repo=request_repo,
        reservation_repo=reservation_repo,
        attempt_repo=attempt_repo,
        health_manager=health_manager,
    )
    application.state.coordinator = coordinator

    catalog.cache.load_model(
        model_id="gpt-4",
        display_name="GPT-4",
        protocol="openai",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("gpt-4", "test-acct")

    yield application

    await httpx_client.aclose()
    await db.disconnect()


# ─── API key end-to-end flow ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_api_key_flows_to_upstream_authorization_header(
    app: FastAPI,
) -> None:
    """The API key from config.toml appears as Bearer token upstream.

    This proves the full chain:
    connect writes api_key → config.toml → AppConfig loads it →
    AccountRegistry stores it → RequestCoordinator fetches it →
    filter_request_headers injects Authorization: Bearer <key>
    """
    captured_requests: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "cmpl-1",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    try:
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_capture)
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "ping"}],
                },
                headers={"Authorization": f"Bearer {LOCAL_KEY}"},
            )

        assert response.status_code == 200
        assert len(captured_requests) == 1

        upstream_headers = captured_requests[0].headers
        auth = upstream_headers.get("authorization", "")

        # Upstream receives the provider account key, not the local server key
        assert auth == f"Bearer {UPSTREAM_KEY}"
        assert LOCAL_KEY not in auth
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_local_credentials_stripped_before_upstream(
    app: FastAPI,
) -> None:
    """Local Authorization/X-Api-Key/Proxy-Authorization are stripped."""
    captured_requests: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "cmpl-2",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    try:
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_capture)
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "ping"}],
                },
                headers={
                    "Authorization": f"Bearer {LOCAL_KEY}",
                    "X-Api-Key": "should-be-stripped",
                    "Proxy-Authorization": "Basic should-be-stripped",
                },
            )

        assert response.status_code == 200
        assert len(captured_requests) == 1

        upstream_headers = captured_requests[0].headers
        # Only one Authorization header with the provider key
        auth_headers = [
            (k, v) for k, v in upstream_headers.items() if k.lower() == "authorization"
        ]
        assert len(auth_headers) == 1
        assert auth_headers[0][1] == f"Bearer {UPSTREAM_KEY}"

        # No local secrets leaked
        for name, value in upstream_headers.items():
            assert LOCAL_KEY not in value, f"Local key leaked in header {name}"
            assert "should-be-stripped" not in value, (
                f"Stripped value leaked in header {name}"
            )
    finally:
        await client.aclose()


# ─── set_config host/port ──────────────────────────────────────────────


class TestSetConfig:
    """Verify the set command updates config and signals server."""

    def test_update_server_config_port_writes_integer(self, tmp_path):
        """Port value is written as unquoted integer in TOML."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[server]\napi_key = "test"\nport = 8080\nhost = "127.0.0.1"\n'
        )

        _update_server_config(str(config_path), "port", "9090")

        content = config_path.read_text()
        assert "port = 9090" in content
        assert '"9090"' not in content  # must NOT be quoted

        # Verify it parses correctly
        config = AppConfig.from_toml(str(config_path))
        assert config.server.port == 9090

    def test_update_server_config_host_writes_quoted(self, tmp_path):
        """Host value is written as quoted string in TOML."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[server]\napi_key = "test"\nport = 8080\nhost = "127.0.0.1"\n'
        )

        _update_server_config(str(config_path), "host", "0.0.0.0")

        content = config_path.read_text()
        assert 'host = "0.0.0.0"' in content

        config = AppConfig.from_toml(str(config_path))
        assert config.server.host == "0.0.0.0"

    def test_update_server_config_escapes_host(self, tmp_path):
        """Host values are escaped instead of corrupting the TOML file."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[server]\nport=8080\nhost="127.0.0.1"\n')

        _update_server_config(str(config_path), "host", 'local"host\\name')

        config = AppConfig.from_toml(str(config_path))
        assert config.server.host == 'local"host\\name'

    @pytest.mark.parametrize("port", ["not-a-port", "-1", "65536"])
    def test_update_server_config_rejects_invalid_port(self, tmp_path, port):
        """Invalid ports fail before the config file is mutated."""
        config_path = tmp_path / "config.toml"
        original = '[server]\nport = 8080\nhost = "127.0.0.1"\n'
        config_path.write_text(original)

        with pytest.raises(SystemExit):
            _update_server_config(str(config_path), "port", port)

        assert config_path.read_text() == original

    def test_set_config_signals_restart(self, tmp_path, monkeypatch):
        """set command writes config and restarts the running server."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[server]\napi_key = "test"\nport = 8080\nhost = "127.0.0.1"\n'
        )

        from click.testing import CliRunner

        runner = CliRunner()
        signaled: list[str] = []

        def fake_restart(_config_path: str):
            signaled.append("restart")
            return True

        monkeypatch.setattr("eggpool.providers.connect.restart_server", fake_restart)

        result = runner.invoke(
            cli,
            ["--config", str(config_path), "set", "port", "3000"],
        )

        assert result.exit_code == 0
        assert "Set port = 3000" in result.output
        assert "Server restarted" in result.output
        assert signaled == ["restart"]

        # Config was actually updated
        config = AppConfig.from_toml(str(config_path))
        assert config.server.port == 3000

    def test_set_config_does_not_fall_back_to_live_reload(self, tmp_path, monkeypatch):
        """set reports a stopped server when restart signaling fails."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[server]\napi_key = "test"\nport = 8080\nhost = "127.0.0.1"\n'
        )

        from click.testing import CliRunner

        runner = CliRunner()

        monkeypatch.setattr(
            "eggpool.providers.connect.restart_server", lambda _path: False
        )

        result = runner.invoke(
            cli,
            ["--config", str(config_path), "set", "host", "0.0.0.0"],
        )

        assert result.exit_code == 0
        assert "Server is not running" in result.output

    def test_set_config_no_server_running(self, tmp_path, monkeypatch):
        """set command reports server not running when no PID file."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[server]\napi_key = "test"\nport = 8080\nhost = "127.0.0.1"\n'
        )

        from click.testing import CliRunner

        runner = CliRunner()

        monkeypatch.setattr(
            "eggpool.providers.connect.restart_server", lambda _path: False
        )

        result = runner.invoke(
            cli,
            ["--config", str(config_path), "set", "port", "4000"],
        )

        assert result.exit_code == 0
        assert "Server is not running" in result.output

        # Config was still updated
        config = AppConfig.from_toml(str(config_path))
        assert config.server.port == 4000


# ─── newkey signals reload ─────────────────────────────────────────────


class TestNewkeySignalsRestart:
    """Verify newkey generates key, saves it, and requests a restart."""

    def test_newkey_signals_restart(self, tmp_path, monkeypatch):
        """newkey writes the new key and requests a restart.

        The previous key is only printed in full when ``--show-old`` is
        passed; the default output redacts it to avoid leaking a key
        that may have just been rotated for security reasons.
        """
        config_path = tmp_path / "config.toml"
        config_path.write_text('[server]\napi_key = "ep_old_key_12345"\nport = 8080\n')

        from click.testing import CliRunner

        runner = CliRunner()
        signaled: list[str] = []

        def fake_restart(_config_path: str):
            signaled.append("restart")
            return True

        monkeypatch.setattr("eggpool.providers.connect.restart_server", fake_restart)

        result = runner.invoke(
            cli,
            ["--config", str(config_path), "newkey"],
        )

        assert result.exit_code == 0
        # Full previous key is redacted by default.
        assert "ep_old_key_12345" not in result.output
        assert "Old key (expired, redacted):" in result.output
        assert "New key (use this): ep_" in result.output
        assert "Server restarted" in result.output
        assert signaled == ["restart"]

        # Verify new key was saved
        config = AppConfig.from_toml(str(config_path))
        assert config.server.api_key is not None
        assert config.server.api_key.startswith("ep_")
        assert config.server.api_key != "ep_old_key_12345"

    def test_newkey_restart_requested(self, tmp_path, monkeypatch):
        """newkey reports a successful restart request."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[server]\napi_key = "ep_old_key"\nport = 8080\n')

        from click.testing import CliRunner

        runner = CliRunner()

        monkeypatch.setattr(
            "eggpool.providers.connect.restart_server", lambda _path: True
        )

        result = runner.invoke(
            cli,
            ["--config", str(config_path), "newkey"],
        )

        assert result.exit_code == 0
        assert "Server restarted" in result.output

    def test_newkey_no_server(self, tmp_path, monkeypatch):
        """newkey reports server not running when no PID file."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[server]\napi_key = "ep_old_key"\nport = 8080\n')

        from click.testing import CliRunner

        runner = CliRunner()

        monkeypatch.setattr(
            "eggpool.providers.connect.restart_server", lambda _path: False
        )

        result = runner.invoke(
            cli,
            ["--config", str(config_path), "newkey"],
        )

        assert result.exit_code == 0
        assert "Server is not running" in result.output


# ─── configsetup ────────────────────────────────────────────────────────


class TestConfigSetup:
    """Verify configsetup commands produce valid configs and auto-generate keys."""

    def test_configsetup_opencode_with_existing_key(self, tmp_path):
        """configsetup opencode uses existing key and LAN IP."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[server]\napi_key = "ep_existing_key_123"\nport = 8080\n'
        )

        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_path),
                "configsetup",
                "opencode",
            ],
        )

        assert result.exit_code == 0
        assert "ep_existing_key_123" in result.stdout
        assert "8080" in result.stdout
        # Should use LAN IP, not localhost
        assert "localhost" not in result.stdout

    def test_configsetup_opencode_auto_generates_key(self, tmp_path):
        """configsetup opencode auto-generates key if not present."""
        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\nport = 9090\n")

        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_path),
                "configsetup",
                "opencode",
            ],
        )

        assert result.exit_code == 0
        # Key is in the snippet
        assert "ep_" in result.stdout
        assert "9090" in result.stdout

        # Verify key was written to config
        key = _read_server_api_key(str(config_path))
        assert key.startswith("ep_")

    def test_configsetup_opencode_valid_json(self, tmp_path):
        """configsetup opencode produces valid JSON."""
        import json

        config_path = tmp_path / "config.toml"
        config_path.write_text('[server]\napi_key = "ep_test_key"\nport = 11300\n')

        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_path),
                "configsetup",
                "opencode",
            ],
        )

        assert result.exit_code == 0
        # stdout contains only the JSON snippet
        snippet = json.loads(result.stdout)

        assert snippet["provider"]["eggpool"]["options"]["apiKey"] == "ep_test_key"
        assert "11300" in snippet["provider"]["eggpool"]["options"]["baseURL"]

    def test_configsetup_opencode_collapse_models_branch(self, tmp_path) -> None:
        """``configsetup opencode`` branches on ``models.collapse_models``:

        - When false (the default), the generated ``models`` map uses
          provider-suffixed IDs (e.g. ``gpt-4/opencode-go``).
        - When true, the generated ``models`` map uses unsuffixed IDs
          (e.g. ``gpt-4``) sourced from the global ``models`` table.

        Reproduces plan lines 121-128.
        """
        import asyncio
        import json

        from click.testing import CliRunner

        from eggpool.db.connection import Database
        from eggpool.db.migrations import MigrationRunner

        db_path = tmp_path / "eggpool.db"

        # Seed the on-disk database with the catalog rows that exercise
        # both branches.
        async def _seed() -> None:
            db = Database(path=str(db_path))
            await db.connect()
            try:
                migration_runner = MigrationRunner(db)
                await migration_runner.run()
                async with db.transaction():
                    await db.execute_write(
                        "INSERT INTO accounts (name, api_key_env, enabled, "
                        "weight, provider_id) VALUES (?, ?, 1, 1.0, ?)",
                        ("oc-acct", "OC_KEY", "opencode-go"),
                    )
                    await db.execute_write(
                        "INSERT INTO accounts (name, api_key_env, enabled, "
                        "weight, provider_id) VALUES (?, ?, 1, 1.0, ?)",
                        ("mm-acct", "MM_KEY", "minimax"),
                    )
                    await db.execute_write(
                        "INSERT OR IGNORE INTO models (model_id, protocol) "
                        "VALUES (?, ?)",
                        ("gpt-4", "openai"),
                    )
                    await db.execute_write(
                        "INSERT OR IGNORE INTO provider_model_metadata "
                        "(model_id, provider_id, protocol) VALUES (?, ?, ?)",
                        ("gpt-4", "opencode-go", "openai"),
                    )
                    await db.execute_write(
                        "INSERT OR IGNORE INTO provider_model_metadata "
                        "(model_id, provider_id, protocol) VALUES (?, ?, ?)",
                        ("gpt-4", "minimax", "openai"),
                    )
                    await db.execute_write(
                        "INSERT INTO account_models "
                        "(account_id, model_id, enabled) "
                        "SELECT id, ?, 1 FROM accounts WHERE name = ?",
                        ("gpt-4", "oc-acct"),
                    )
                    await db.execute_write(
                        "INSERT INTO account_models "
                        "(account_id, model_id, enabled) "
                        "SELECT id, ?, 1 FROM accounts WHERE name = ?",
                        ("gpt-4", "mm-acct"),
                    )
            finally:
                await db.disconnect()

        asyncio.run(_seed())

        # collapse_models = false → suffixed IDs
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent(f"""\
                [server]
                api_key = "ep_test_key"
                port = 11300

                [models]
                collapse_models = false

                [database]
                path = "{db_path}"
            """)
        )
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "configsetup", "opencode"],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        snippet = json.loads(result.stdout)
        models = snippet["provider"]["eggpool"]["models"]
        assert "gpt-4/opencode-go" in models
        assert "gpt-4/minimax" in models
        assert "gpt-4" not in models

        # collapse_models = true → unsuffixed IDs
        config_path.write_text(
            textwrap.dedent(f"""\
                [server]
                api_key = "ep_test_key"
                port = 11300

                [models]
                collapse_models = true

                [database]
                path = "{db_path}"
            """)
        )
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "configsetup", "opencode"],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        snippet = json.loads(result.stdout)
        models = snippet["provider"]["eggpool"]["models"]
        assert "gpt-4" in models
        assert "gpt-4/opencode-go" not in models
        assert "gpt-4/minimax" not in models

    def test_configsetup_opencode_adds_configured_minimax_static_models(
        self, tmp_path
    ) -> None:
        import json

        from click.testing import CliRunner

        db_path = tmp_path / "eggpool.db"
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent(f"""\
                [server]
                api_key = "ep_test_key"
                port = 11300

                [models]
                collapse_models = false

                [database]
                path = "{db_path}"

                [providers.minimax]
                id = "minimax"
                base_url = "https://api.minimax.io/anthropic"
                protocols = ["anthropic"]
                anthropic_path = "/v1/messages"

                [[providers.minimax.accounts]]
                name = "mm-acct"
                api_key = "sk-mm"

                [providers.minimax.auth]
                mode = "api_key"
                header = "x-api-key"

                [[providers.minimax.static_models]]
                id = "MiniMax-M3"
                display_name = "MiniMax-M3"
                protocol = "anthropic"
                max_context_tokens = 1000000
            """)
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "configsetup", "opencode"],
        )

        assert result.exit_code == 0, result.stdout + result.stderr
        snippet = json.loads(result.stdout)
        models = snippet["provider"]["eggpool"]["models"]
        assert "MiniMax-M3/minimax" in models
        assert "MiniMax-M3" not in models

    def test_configsetup_claude_code_with_existing_key(self, tmp_path):
        """configsetup claude-code uses existing key and LAN IP.

        Note: ``configsetup claude-code`` writes only to the clipboard
        in normal mode; the key is not echoed to stdout. This test
        exists to assert the existing key is *not* leaked into the
        non-JSON status output.
        """
        from unittest.mock import patch

        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[server]\napi_key = "ep_existing_key_456"\nport = 8080\n'
        )

        from click.testing import CliRunner

        captured: dict[str, str] = {}

        def fake_copy(text: str) -> bool:
            captured["text"] = text
            return True

        runner = CliRunner()
        with patch("eggpool.cli_full._copy_to_clipboard", side_effect=fake_copy):
            result = runner.invoke(
                cli,
                ["--config", str(config_path), "configsetup", "claude-code"],
            )

        assert result.exit_code == 0
        # Key must NOT appear in scrollback (B14).
        assert "ep_existing_key_456" not in result.output
        assert "ep_existing_key_456" in captured["text"]
        assert "8080" in captured["text"]

    def test_configsetup_claude_code_auto_generates_key(self, tmp_path):
        """configsetup claude-code auto-generates key if not present."""
        from unittest.mock import patch

        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\nport = 7777\n")

        from click.testing import CliRunner

        captured: dict[str, str] = {}

        def fake_copy(text: str) -> bool:
            captured["text"] = text
            return True

        runner = CliRunner()
        with patch("eggpool.cli_full._copy_to_clipboard", side_effect=fake_copy):
            result = runner.invoke(
                cli,
                ["--config", str(config_path), "configsetup", "claude-code"],
            )

        assert result.exit_code == 0
        # The auto-generated key must not be echoed to stdout (B14).
        assert "ep_" not in result.output
        assert "ep_" in captured["text"]
        assert "7777" in captured["text"]
        # Verify key was written to config
        key = _read_server_api_key(str(config_path))
        assert key.startswith("ep_")

    def test_configsetup_claude_code_valid_json(self, tmp_path):
        """configsetup claude-code writes valid JSON to the clipboard.

        The snippet format is exercised indirectly by mocking
        ``_copy_to_clipboard`` to capture the rendered text, ensuring
        the key never appears in scrollback (B14).
        """
        import json
        from unittest.mock import patch

        config_path = tmp_path / "config.toml"
        config_path.write_text('[server]\napi_key = "ep_claude_key"\nport = 11300\n')

        from click.testing import CliRunner

        captured: dict[str, str] = {}

        def fake_copy(text: str) -> bool:
            captured["text"] = text
            return True

        runner = CliRunner()
        with patch("eggpool.cli_full._copy_to_clipboard", side_effect=fake_copy):
            result = runner.invoke(
                cli,
                ["--config", str(config_path), "configsetup", "claude-code"],
            )

        assert result.exit_code == 0
        assert "ep_claude_key" not in result.output
        assert "text" in captured
        snippet = json.loads(captured["text"])
        assert snippet["api_key"] == "ep_claude_key"
        assert "11300" in snippet["base_url"]

    def test_configsetup_works_with_duplicate_accounts(self, tmp_path):
        """configsetup works even if config has duplicate account names."""
        config_path = tmp_path / "config.toml"
        # Write config with duplicate names (would fail AppConfig validation)
        config_path.write_text(
            textwrap.dedent("""\
                [server]
                api_key = "ep_server_key"
                port = 8080

                [providers.p1]
                id = "p1"
                base_url = "https://example.com"
                protocols = ["openai"]

                [[providers.p1.accounts]]
                name = "default"
                api_key = "key1"

                [providers.p2]
                id = "p2"
                base_url = "https://other.com"
                protocols = ["openai"]

                [[providers.p2.accounts]]
                name = "default"
                api_key = "key2"
            """)
        )

        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_path),
                "configsetup",
                "opencode",
            ],
        )

        # Should succeed despite duplicate account names
        assert result.exit_code == 0
        assert "ep_server_key" in result.stdout

    def test_configsetup_does_not_overwrite_full_config(self, tmp_path):
        """configsetup only adds api_key, does not rewrite the entire file."""
        config_path = tmp_path / "config.toml"
        original_content = textwrap.dedent("""\
            [server]
            port = 8080
            log_level = "DEBUG"

            [upstream]
            base_url = "https://custom.example.com/v1"
        """)
        config_path.write_text(original_content)

        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "configsetup", "opencode"],
        )

        assert result.exit_code == 0

        # Verify the config file still has the original content plus api_key
        content = config_path.read_text()
        assert "port = 8080" in content
        assert 'log_level = "DEBUG"' in content
        assert "custom.example.com" in content
        assert "api_key" in content

    def test_configsetup_lan_ip_detection(self, tmp_path, monkeypatch):
        """configsetup detects LAN IP address."""
        import socket

        from eggpool.cli import _detect_lan_ip

        class FakeSocket:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def connect(self, _address):
                return None

            def getsockname(self):
                return ("192.168.1.25", 12345)

        monkeypatch.setattr(socket, "socket", lambda *_args: FakeSocket())

        lan_ip = _detect_lan_ip()
        # Should be a valid IP address
        parts = lan_ip.split(".")
        assert len(parts) == 4
        assert all(0 <= int(p) <= 255 for p in parts)
        # Should not be localhost
        assert lan_ip != "127.0.0.1"


# ─── No-accounts warnings ──────────────────────────────────────────────


class TestNoAccountsWarning:
    """Verify CLI commands warn appropriately when no accounts exist."""

    def test_serve_warns_no_accounts(self, tmp_path, monkeypatch):
        """serve command warns when no accounts are configured."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[server]\napi_key = "ep_test"\nport = 8080\n')

        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "accounts", "status"],
        )

        assert result.exit_code == 0
        assert "No provider accounts configured" in result.output
        assert "eggpool connect" in result.output

    def test_accounts_status_shows_each_account(self, tmp_path):
        """accounts status lists all configured accounts."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent("""\
                [server]
                api_key = "ep_test"
                port = 8080

                [providers.p1]
                id = "p1"
                base_url = "https://example.com"
                protocols = ["openai"]

                [[providers.p1.accounts]]
                name = "acct1"
                api_key = "key1"

                [[providers.p1.accounts]]
                name = "acct2"
                api_key = "key2"
            """)
        )

        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "accounts", "status"],
        )

        assert result.exit_code == 0
        assert "acct1" in result.output
        assert "acct2" in result.output
        assert "Total accounts: 2" in result.output


# ─── Config example validation ──────────────────────────────────────────


class TestConfigExample:
    """Verify config.example.toml is valid and has no active accounts."""

    def test_config_example_has_no_active_accounts(self):
        """config.example.toml should have all accounts commented out."""
        import tomllib
        from pathlib import Path

        config_path = Path(__file__).parent.parent.parent / "config.example.toml"
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

        # No providers section should exist (all commented out)
        assert "providers" not in raw

    def test_config_example_valid_toml(self):
        """config.example.toml should parse as valid TOML."""
        import tomllib
        from pathlib import Path

        config_path = Path(__file__).parent.parent.parent / "config.example.toml"
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

        assert "server" in raw
        assert "upstream" in raw
        assert raw["server"]["port"] == 11300

    def test_config_example_loads_with_app_config(self):
        """config.example.toml should be valid AppConfig."""
        from pathlib import Path

        from eggpool.models.config import AppConfig

        config_path = Path(__file__).parent.parent.parent / "config.example.toml"
        config = AppConfig.from_toml(str(config_path))
        assert config.server.port == 11300
        assert len(config.all_accounts()) == 0
