"""End-to-end tests confirming real API keys flow through all stages.

Verifies:
- connect command saves real API key (not placeholder) to config
- Server API key is generated and used for auth
- Provider API key is forwarded to upstream
- getkey/newkey commands work correctly
- configsetup commands produce valid config snippets
"""

from __future__ import annotations

import json
import textwrap
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from eggpool.app import create_app
from eggpool.cli import _read_server_api_key, cli, generate_api_key
from eggpool.models.config import AppConfig
from eggpool.providers.connect import merge_provider_into_config

if TYPE_CHECKING:
    from pathlib import Path

UPSTREAM_BASE = "https://test-upstream.example.com"
SERVER_KEY = "ep_test_server_key_abcdef1234567890abcdef"
PROVIDER_KEY = "sk-provider-real-key-xyz123"


class TestConnectSavesRealApiKey:
    """Verify the connect flow writes real API keys, not placeholders."""

    def test_connect_writes_real_key_to_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """merge_provider_into_config writes the actual key value."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent("""\
                [server]
                api_key = "ep_server_key"
                port = 11300
            """)
        )

        provider_data = {
            "id": "opencode-go",
            "base_url": "https://opencode.ai/zen/go/v1",
            "protocols": ["openai", "anthropic"],
        }

        ok = merge_provider_into_config(str(config_path), provider_data, PROVIDER_KEY)
        assert ok is True

        content = config_path.read_text(encoding="utf-8")
        assert f'api_key = "{PROVIDER_KEY}"' in content
        # Must NOT contain placeholder
        assert "your-api-key" not in content
        assert "API_KEY" not in content.split("\n")[-1]  # not a placeholder env var

    def test_config_roundtrip_preserves_real_key(
        self,
        tmp_path: Path,
    ) -> None:
        """Config with inline api_key can be parsed back correctly."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent(f"""\
                [server]
                api_key = "{SERVER_KEY}"
                port = 11300

                [providers.opencode-go]
                id = "opencode-go"
                base_url = "https://opencode.ai/zen/go/v1"
                protocols = ["openai"]

                [[providers.opencode-go.accounts]]
                name = "default"
                api_key = "{PROVIDER_KEY}"
            """)
        )

        config = AppConfig.from_toml(str(config_path))
        assert config.server.api_key == SERVER_KEY
        assert config.server.resolved_api_key == SERVER_KEY

        provider = config.providers["opencode-go"]
        assert provider.accounts[0].api_key == PROVIDER_KEY


class TestServerApiKeyAuth:
    """Verify the server API key is used for authentication end-to-end."""

    @pytest.mark.asyncio
    async def test_server_api_key_auth_flow(
        self,
        tmp_path: Path,
    ) -> None:
        """Request with correct server API key gets through; wrong key gets 401."""
        config = AppConfig.from_dict(
            {
                "server": {
                    "api_key": SERVER_KEY,
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

        with respx.mock:
            respx.get(f"{UPSTREAM_BASE}/models").mock(
                return_value=httpx.Response(200, json={"data": []})
            )

            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as client,
            ):
                # Correct key → 200 (or 502 if upstream unreachable, but not 401)
                resp = await client.get(
                    "/v1/models",
                    headers={"Authorization": f"Bearer {SERVER_KEY}"},
                )
                assert resp.status_code != 401

                # Wrong key → 401
                resp = await client.get(
                    "/v1/models",
                    headers={"Authorization": "Bearer wrong-key"},
                )
                assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_x_api_key_header_works(
        self,
        tmp_path: Path,
    ) -> None:
        """x-api-key header is accepted as an alternative to Authorization."""
        config = AppConfig.from_dict(
            {
                "server": {
                    "api_key": SERVER_KEY,
                    "host": "127.0.0.1",
                    "port": 0,
                },
                "database": {"path": str(tmp_path / "xapikey.sqlite3")},
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

        with respx.mock:
            respx.get(f"{UPSTREAM_BASE}/models").mock(
                return_value=httpx.Response(200, json={"data": []})
            )

            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as client,
            ):
                resp = await client.get(
                    "/v1/models",
                    headers={"x-api-key": SERVER_KEY},
                )
                assert resp.status_code != 401

                resp = await client.get(
                    "/v1/models",
                    headers={"x-api-key": "wrong-key"},
                )
                assert resp.status_code == 401


class TestProviderKeyForwarded:
    """Verify the provider API key is forwarded to upstream."""

    @pytest.mark.asyncio
    async def test_provider_key_appears_in_upstream_request(
        self,
        tmp_path: Path,
    ) -> None:
        """The provider API key is sent to the upstream, not the server key."""
        config = AppConfig.from_dict(
            {
                "server": {
                    "api_key": SERVER_KEY,
                    "host": "127.0.0.1",
                    "port": 0,
                },
                "database": {"path": str(tmp_path / "fwd.sqlite3")},
                "upstream": {"base_url": UPSTREAM_BASE},
                "models": {
                    "startup_refresh": False,
                    "refresh_interval_s": 0,
                },
                "accounts": [
                    {"name": "test-acct", "api_key": PROVIDER_KEY},
                ],
                "dashboard": {"enabled": False},
            }
        )

        app = create_app(config)
        captured_headers: dict[str, str] = {}

        def _capture_handler(request: httpx.Request) -> httpx.Response:
            captured_headers.update(dict(request.headers))
            return httpx.Response(200, json={"choices": []})

        # Flat config auto-normalizes opencode-go provider with upstream.base_url
        with respx.mock:
            respx.route(method="POST", url__regex=r".*chat/completions").mock(
                side_effect=_capture_handler
            )

            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as client,
            ):
                # Seed the catalog (in-memory cache + DB) so routing works
                app.state.catalog.cache.update_from_account(
                    "test-acct",
                    "opencode-go",
                    [
                        {
                            "model_id": "test-model",
                            "protocol": "openai",
                            "protocol_source": "explicit",
                            "display_name": "Test Model",
                            "capabilities": {},
                            "source_metadata": {},
                        }
                    ],
                )
                # Also insert into the models DB table (FK constraint)
                db = app.state.db
                async with db.transaction():
                    await db.execute_write(
                        """
                        INSERT OR IGNORE INTO models
                            (model_id, protocol, protocol_source)
                        VALUES (?, 'openai', 'explicit')
                        """,
                        ("test-model",),
                    )

                resp = await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": f"Bearer {SERVER_KEY}"},
                    json={
                        "model": "test-model",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                )
                assert resp.status_code == 200

                # The upstream should receive the provider key, not the server key
                auth = captured_headers.get("authorization", "")
                assert PROVIDER_KEY in auth
                assert SERVER_KEY not in auth


class TestGetkeyNewkey:
    """Verify getkey and newkey CLI commands."""

    def test_getkey_prints_current_key(self, tmp_path: Path) -> None:
        """getkey prints the current server API key."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent(f"""\
                [server]
                api_key = "{SERVER_KEY}"
                port = 11300
            """)
        )

        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "getkey"],
        )

        assert result.exit_code == 0
        assert result.output == SERVER_KEY

    def test_newkey_generates_and_prints(self, tmp_path: Path) -> None:
        """newkey generates a new key, prints old (redacted) and new.

        The previous key is only printed in full when ``--show-old`` is
        passed, to avoid leaking a key that may have just been rotated
        for security reasons.
        """
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent(f"""\
                [server]
                api_key = "{SERVER_KEY}"
                port = 11300
            """)
        )

        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "newkey"],
        )

        assert result.exit_code == 0
        # Old key is redacted by default - the full secret must not
        # appear in stdout.
        assert SERVER_KEY not in result.output
        assert "Old key (expired, redacted):" in result.output

        # The new key should be different and saved to config
        new_key = _read_server_api_key(str(config_path))
        assert new_key != SERVER_KEY
        assert new_key.startswith("ep_")
        assert new_key in result.output

    def test_newkey_show_old_prints_full_previous_key(self, tmp_path: Path) -> None:
        """``--show-old`` prints the full previous key for confirmation."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent(f"""\
                [server]
                api_key = "{SERVER_KEY}"
                port = 11300
            """)
        )

        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "newkey", "--show-old"],
        )

        assert result.exit_code == 0
        assert SERVER_KEY in result.output

    def test_generate_api_key_format(self) -> None:
        """Generated keys have the expected format."""
        key = generate_api_key()
        assert key.startswith("ep_")
        assert len(key) == 67  # "ep_" + 64 hex chars

    def test_newkey_preserves_api_key_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression test: ``eggpool newkey`` must not clobber
        ``api_key_env`` declarations in the [server] section.
        """
        monkeypatch.setenv("EGGPOOL_API_KEY", "env-sourced-server-key")
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent("""\
                [server]
                api_key_env = "EGGPOOL_API_KEY"
                port = 11300
            """)
        )

        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "newkey"],
        )

        assert result.exit_code == 0, result.output

        written = config_path.read_text(encoding="utf-8")
        assert "api_key_env" in written
        assert "EGGPOOL_API_KEY" in written
        assert 'api_key = "' not in written


class TestConfigSetup:
    """Verify configsetup commands produce valid snippets."""

    def test_configsetup_opencode(
        self,
        tmp_path: Path,
    ) -> None:
        """configsetup opencode produces valid JSON with the real key."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent(f"""\
                [server]
                api_key = "{SERVER_KEY}"
                port = 11300
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

        assert result.exit_code == 0
        assert SERVER_KEY in result.stdout

        # stdout is the JSON snippet
        snippet = json.loads(result.stdout)
        assert snippet["provider"]["eggpool"]["options"]["apiKey"] == SERVER_KEY
        assert "11300" in snippet["provider"]["eggpool"]["options"]["baseURL"]

    def test_configsetup_claude_code(
        self,
        tmp_path: Path,
    ) -> None:
        """configsetup claude-code writes the key-bearing snippet to clipboard.

        The key is no longer echoed to stdout (B14); capture the
        clipboard payload instead.
        """
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent(f"""\
                [server]
                api_key = "{SERVER_KEY}"
                port = 11300
            """)
        )

        from unittest.mock import patch

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
        assert SERVER_KEY not in result.output
        assert "text" in captured
        snippet = json.loads(captured["text"])
        assert snippet["api_key"] == SERVER_KEY
        assert "11300" in snippet["base_url"]


class TestApiKeyNotPlaceholder:
    """Verify no placeholder keys leak into config or requests."""

    def test_no_placeholder_in_connect_output(
        self,
        tmp_path: Path,
    ) -> None:
        """After merge, config contains the real key, not a placeholder."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent("""\
                [server]
                port = 11300
            """)
        )

        real_key = "sk-my-real-api-key-abc123def456"
        merge_provider_into_config(
            str(config_path),
            {"id": "test", "base_url": "https://test.example.com"},
            real_key,
        )

        config = AppConfig.from_toml(str(config_path))
        acct = config.providers["test"].accounts[0]
        assert acct.api_key == real_key
        # Verify it is NOT a placeholder
        placeholders = {
            "your-api-key-here",
            "your-proxy-api-key",
            "your-opencode-go-key-1",
            "your-opencode-go-key-2",
            "your-local-api-key-here",
        }
        assert real_key.lower() not in placeholders
