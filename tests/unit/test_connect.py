"""Tests for the provider connect module."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from go_aggregator.cli import cli
from go_aggregator.providers.connect import (
    ConfiguredAccount,
    _extract_raw_block,
    _format_provider_block,
    _provider_account_count,
    _provider_id_to_env_name,
    _toml_value,
    _unique_account_name,
    _unique_env_name,
    export_env_var,
    load_provider_templates,
    merge_provider_into_config,
    remove_account_from_config,
)


class TestLoadProviderTemplates:
    """Tests for loading provider templates from TOML."""

    @pytest.mark.asyncio()
    async def test_loads_all_providers(self, tmp_path: Path) -> None:
        """All providers in the template file are loaded."""
        providers_toml = tmp_path / "providers.toml"
        providers_toml.write_text(
            textwrap.dedent("""\
                [providers.alpha]
                id = "alpha"
                base_url = "https://alpha.example.com"
                protocols = ["openai"]
                api_key_env = "API_KEY"

                [providers.beta]
                id = "beta"
                base_url = "https://beta.example.com"
                protocols = ["openai", "anthropic"]
                api_key_env = "API_KEY"
            """)
        )

        templates = load_provider_templates(str(providers_toml))
        assert len(templates) == 2
        assert "alpha" in templates
        assert "beta" in templates

    @pytest.mark.asyncio()
    async def test_template_data_fields(self, tmp_path: Path) -> None:
        """Each template contains display, url, raw, and data keys."""
        providers_toml = tmp_path / "providers.toml"
        providers_toml.write_text(
            textwrap.dedent("""\
                [providers.my-provider]
                id = "my-provider"
                base_url = "https://my-provider.example.com"
                protocols = ["openai"]
                api_key_env = "API_KEY"
            """)
        )

        templates = load_provider_templates(str(providers_toml))
        tmpl = templates["my-provider"]
        assert tmpl["display"] == "my-provider"
        assert tmpl["url"] == "https://my-provider.example.com"
        assert isinstance(tmpl["raw"], str)
        assert isinstance(tmpl["data"], dict)
        assert tmpl["data"]["id"] == "my-provider"

    @pytest.mark.asyncio()
    async def test_empty_when_file_missing(self, tmp_path: Path) -> None:
        """Returns empty dict when file doesn't exist."""
        templates = load_provider_templates(str(tmp_path / "nonexistent.toml"))
        assert templates == {}

    @pytest.mark.asyncio()
    async def test_empty_when_no_providers(self, tmp_path: Path) -> None:
        """Returns empty dict when file has no [providers.*] sections."""
        providers_toml = tmp_path / "providers.toml"
        providers_toml.write_text('[server]\nhost = "localhost"\n')
        templates = load_provider_templates(str(providers_toml))
        assert templates == {}


class TestExtractRawBlock:
    """Tests for extracting raw TOML blocks."""

    def test_extracts_block(self) -> None:
        """Extracts the correct [providers.X] block."""
        text = textwrap.dedent("""\
            [providers.alpha]
            id = "alpha"
            base_url = "https://alpha.example.com"

            [providers.beta]
            id = "beta"
            base_url = "https://beta.example.com"
        """)
        block = _extract_raw_block(text, "alpha")
        assert "[providers.alpha]" in block
        assert "alpha.example.com" in block
        assert "beta" not in block

    def test_extracts_last_block(self) -> None:
        """Extracts the last block when there are multiple."""
        text = textwrap.dedent("""\
            [providers.alpha]
            id = "alpha"

            [providers.beta]
            id = "beta"
        """)
        block = _extract_raw_block(text, "beta")
        assert "[providers.beta]" in block

    def test_empty_string_when_not_found(self) -> None:
        """Returns empty string when provider not found."""
        text = '[providers.alpha]\nid = "alpha"\n'
        block = _extract_raw_block(text, "gamma")
        assert block == ""


class TestProviderIdToEnvName:
    """Tests for converting provider IDs to env var names."""

    def test_simple_id(self) -> None:
        """Simple ID becomes uppercase with _API_KEY suffix."""
        assert _provider_id_to_env_name("openai") == "OPENAI_API_KEY"

    def test_hyphenated_id(self) -> None:
        """Hyphens become underscores."""
        assert _provider_id_to_env_name("opencode-go") == "OPENCODE_GO_API_KEY"

    def test_ollama_local(self) -> None:
        """Ollama local becomes OLLAMA_LOCAL_API_KEY."""
        assert _provider_id_to_env_name("ollama-local") == "OLLAMA_LOCAL_API_KEY"


class TestUniqueAccountName:
    """Tests for generating unique account names."""

    def test_first_account(self) -> None:
        """First account gets 'default'."""
        assert _unique_account_name("minimax", []) == "default"

    def test_second_account(self) -> None:
        """Second account gets 'default-2'."""
        assert _unique_account_name("minimax", ["default"]) == "default-2"

    def test_third_account(self) -> None:
        """Third account gets 'default-3'."""
        names = ["default", "default-2"]
        assert _unique_account_name("minimax", names) == "default-3"

    def test_preserves_existing_names(self) -> None:
        """Skips existing numbered names."""
        names = ["default", "default-2", "default-4"]
        assert _unique_account_name("minimax", names) == "default-3"


class TestUniqueEnvName:
    """Tests for generating unique environment variable names."""

    def test_first_account(self) -> None:
        """First account uses base env name."""
        assert _unique_env_name("minimax", []) == "MINIMAX_API_KEY"

    def test_second_account(self) -> None:
        """Second account appends _2."""
        assert _unique_env_name("minimax", ["default"]) == "MINIMAX_API_KEY_2"

    def test_third_account(self) -> None:
        """Third account appends _3."""
        names = ["default", "default-2"]
        assert _unique_env_name("minimax", names) == "MINIMAX_API_KEY_3"


class TestTomlValue:
    """Tests for formatting Python values as TOML."""

    def test_bool_true(self) -> None:
        assert _toml_value(True) == "true"

    def test_bool_false(self) -> None:
        assert _toml_value(False) == "false"

    def test_int(self) -> None:
        assert _toml_value(42) == "42"

    def test_float(self) -> None:
        assert _toml_value(3.14) == "3.14"

    def test_string(self) -> None:
        assert _toml_value("hello") == '"hello"'

    def test_list_of_strings(self) -> None:
        result = _toml_value(["openai", "anthropic"])
        assert result == '["openai", "anthropic"]'

    def test_empty_list(self) -> None:
        assert _toml_value([]) == "[]"


class TestFormatProviderBlock:
    """Tests for formatting provider blocks as TOML."""

    def test_basic_block(self) -> None:
        """Formats a basic provider block."""
        data = {
            "base_url": "https://example.com",
            "protocols": ["openai"],
        }
        block = _format_provider_block("my-provider", data, "MY_API_KEY")
        assert "[providers.my-provider]" in block
        assert 'base_url = "https://example.com"' in block
        assert 'protocols = ["openai"]' in block
        assert "[[providers.my-provider.accounts]]" in block
        assert 'api_key_env = "MY_API_KEY"' in block

    def test_non_default_fields(self) -> None:
        """Includes non-default fields like openai_path."""
        data = {
            "base_url": "https://example.com",
            "protocols": ["openai", "anthropic"],
            "openai_path": "/v1/chat/completions",
            "anthropic_path": "/anthropic/v1/messages",
        }
        block = _format_provider_block("test", data, "KEY")
        assert 'openai_path = "/v1/chat/completions"' in block
        assert 'anthropic_path = "/anthropic/v1/messages"' in block

    def test_excludes_id_and_accounts(self) -> None:
        """Does not include id or accounts in the output."""
        data = {
            "id": "should-be-excluded",
            "base_url": "https://example.com",
            "accounts": [],
        }
        block = _format_provider_block("test", data, "KEY")
        assert "should-be-excluded" not in block.split("\n")[0]


class TestMergeProviderIntoConfig:
    """Tests for merging providers into config files."""

    def test_adds_new_provider(self, tmp_path: Path) -> None:
        """Adds a new provider block to config."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            textwrap.dedent("""\
                [server]
                port = 8080

                [providers.existing]
                id = "existing"
                base_url = "https://existing.example.com"
            """)
        )

        provider_data = {
            "id": "new-provider",
            "base_url": "https://new.example.com",
            "protocols": ["openai"],
        }

        ok = merge_provider_into_config(str(config_file), provider_data, "NEW_API_KEY")
        assert ok is True

        content = config_file.read_text()
        assert "[providers.new-provider]" in content
        assert "new.example.com" in content
        assert 'api_key_env = "NEW_API_KEY"' in content

    def test_appends_account_to_existing_provider(self, tmp_path: Path) -> None:
        """Appends a new account to an existing provider."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            textwrap.dedent("""\
                [providers.minimax]
                id = "minimax"
                base_url = "https://api.minimaxi.com"

                [[providers.minimax.accounts]]
                name = "default"
                api_key_env = "MINIMAX_API_KEY"
            """)
        )

        provider_data = {
            "id": "minimax",
            "base_url": "https://api.minimaxi.com",
            "protocols": ["openai"],
        }

        ok = merge_provider_into_config(
            str(config_file), provider_data, "MINIMAX_API_KEY_2"
        )
        assert ok is True

        content = config_file.read_text()
        # Should have two account blocks
        assert content.count("[[providers.minimax.accounts]]") == 2
        assert 'name = "default-2"' in content
        assert 'api_key_env = "MINIMAX_API_KEY_2"' in content

    def test_preserves_existing_config(self, tmp_path: Path) -> None:
        """Preserves existing config sections when adding a provider."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            textwrap.dedent("""\
                [server]
                port = 8080

                [database]
                path = "data.sqlite3"
            """)
        )

        provider_data = {
            "id": "test",
            "base_url": "https://test.example.com",
        }

        merge_provider_into_config(str(config_file), provider_data, "TEST_KEY")

        content = config_file.read_text()
        assert "port = 8080" in content
        assert 'path = "data.sqlite3"' in content

    def test_returns_false_when_file_missing(self, tmp_path: Path) -> None:
        """Returns False when config file doesn't exist."""
        ok = merge_provider_into_config(
            str(tmp_path / "nonexistent.toml"),
            {"id": "test", "base_url": "https://test.example.com"},
            "KEY",
        )
        assert ok is False


class TestRemoveAccountFromConfig:
    """Tests for removing provider accounts from config files."""

    def test_removes_account_and_keeps_provider_with_remaining_account(
        self,
        tmp_path: Path,
    ) -> None:
        """Removing one of two accounts leaves the provider block."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            textwrap.dedent("""\
                [providers.opencode-go]
                id = "opencode-go"
                base_url = "https://api.example.com"

                [[providers.opencode-go.accounts]]
                name = "default"
                api_key_env = "OPENCODE_GO_API_KEY"

                [[providers.opencode-go.accounts]]
                name = "default-2"
                api_key_env = "OPENCODE_GO_API_KEY_2"
            """),
            encoding="utf-8",
        )

        ok = remove_account_from_config(
            str(config_file),
            ConfiguredAccount(
                provider_id="opencode-go",
                name="default",
                api_key_env="OPENCODE_GO_API_KEY",
                api_key=None,
            ),
        )

        assert ok is True
        content = config_file.read_text(encoding="utf-8")
        assert "[providers.opencode-go]" in content
        assert 'name = "default"' not in content
        assert 'name = "default-2"' in content
        assert _provider_account_count(content, "opencode-go") == 1

    def test_removes_provider_when_final_account_removed(
        self,
        tmp_path: Path,
    ) -> None:
        """Removing the last account removes the provider section."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            textwrap.dedent("""\
                [server]
                port = 8080

                [providers.opencode-go]
                id = "opencode-go"
                base_url = "https://api.example.com"

                [[providers.opencode-go.accounts]]
                name = "default"
                api_key_env = "OPENCODE_GO_API_KEY"

                [providers.other]
                id = "other"
                base_url = "https://other.example.com"

                [[providers.other.accounts]]
                name = "other-default"
                api_key_env = "OTHER_API_KEY"
            """),
            encoding="utf-8",
        )

        ok = remove_account_from_config(
            str(config_file),
            ConfiguredAccount(
                provider_id="opencode-go",
                name="default",
                api_key_env="OPENCODE_GO_API_KEY",
                api_key=None,
            ),
        )

        assert ok is True
        content = config_file.read_text(encoding="utf-8")
        assert "[server]" in content
        assert "[providers.opencode-go]" not in content
        assert "OPENCODE_GO_API_KEY" not in content
        assert "[providers.other]" in content
        assert "OTHER_API_KEY" in content


class TestExportEnvVar:
    """Tests for exporting environment variables to shell profile."""

    def test_creates_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Creates a new profile file with export statement."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("SHELL", "/bin/zsh")

        profile = export_env_var("TEST_API_KEY", "sk-test-123")
        assert profile is not None
        assert profile.exists()

        content = profile.read_text()
        assert "export TEST_API_KEY=sk-test-123" in content

    def test_replaces_existing_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Replaces an existing env var in the profile."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("SHELL", "/bin/zsh")

        profile = tmp_path / ".zshrc"
        profile.write_text("export OLD_KEY=old-value\nexport TEST_KEY=old-value\n")

        export_env_var("TEST_KEY", "new-value")

        content = profile.read_text()
        assert "export TEST_KEY=new-value" in content
        assert "export OLD_KEY=old-value" in content
        # Should not have duplicate entries
        assert content.count("export TEST_KEY=") == 1

    def test_appends_to_existing_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Appends to an existing profile file."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("SHELL", "/bin/zsh")

        profile = tmp_path / ".zshrc"
        profile.write_text("export PATH=/usr/bin\n")

        export_env_var("NEW_KEY", "new-value")

        content = profile.read_text()
        assert "export PATH=/usr/bin" in content
        assert "export NEW_KEY=new-value" in content


class TestCliConnectList:
    """Tests for ``connect list``."""

    def test_connect_list_displays_templates(self, tmp_path: Path) -> None:
        """Lists providers from the providers template file."""
        providers_toml = tmp_path / "providers.toml"
        providers_toml.write_text(
            textwrap.dedent("""\
                [providers.opencode-go]
                _display = "OpenCode Go"
                base_url = "https://api.example.com"
                protocols = ["openai"]
            """),
            encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["connect", "--providers", str(providers_toml), "list"],
        )

        assert result.exit_code == 0
        assert "Available providers:" in result.stdout
        assert "opencode-go: OpenCode Go" in result.stdout


class TestCliLogout:
    """Tests for ``logout`` command behavior."""

    def test_logout_by_api_key_removes_matching_account(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A direct API key argument removes the account using that key."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent("""\
                [providers.opencode-go]
                id = "opencode-go"
                base_url = "https://api.example.com"

                [[providers.opencode-go.accounts]]
                name = "default"
                api_key_env = "OPENCODE_GO_API_KEY"
            """),
            encoding="utf-8",
        )
        monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-live-key")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "logout", "sk-live-key"],
        )

        assert result.exit_code == 0
        assert "Removed opencode-go/default" in result.stdout
        assert "[providers.opencode-go]" not in config_path.read_text(encoding="utf-8")

    def test_logout_missing_api_key_reports_not_found(
        self,
        tmp_path: Path,
    ) -> None:
        """Unknown API keys produce a not-found message."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent("""\
                [providers.opencode-go]
                id = "opencode-go"
                base_url = "https://api.example.com"

                [[providers.opencode-go.accounts]]
                name = "default"
                api_key_env = "OPENCODE_GO_API_KEY"
            """),
            encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "logout", "missing-key"],
        )

        assert result.exit_code == 0
        assert "No configured provider or API key found" in result.stdout

    def test_logout_duplicate_provider_uses_selected_account(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Multiple matching provider accounts are resolved by menu selection."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent("""\
                [providers.opencode-go]
                id = "opencode-go"
                base_url = "https://api.example.com"

                [[providers.opencode-go.accounts]]
                name = "default"
                api_key_env = "OPENCODE_GO_API_KEY"

                [[providers.opencode-go.accounts]]
                name = "default-2"
                api_key_env = "OPENCODE_GO_API_KEY_2"
            """),
            encoding="utf-8",
        )
        monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-first-key")
        monkeypatch.setenv("OPENCODE_GO_API_KEY_2", "sk-second-key")

        from go_aggregator.providers import connect as connect_module

        class FakeMenu:
            """Deterministic stand-in for the terminal menu."""

            def __init__(self, title: str, options: list[str]) -> None:
                self.title = title
                self.options = options

            def run(self) -> str:
                return self.options[1]

        monkeypatch.setattr(connect_module, "TerminalMenu", FakeMenu)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "logout", "opencodego"],
        )

        assert result.exit_code == 0
        content = config_path.read_text(encoding="utf-8")
        assert 'name = "default"' in content
        assert 'name = "default-2"' not in content


class TestConfigRefresh:
    """Tests for manual config reload signaling."""

    def test_config_refresh_sends_reload_signal(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent("""\
                [providers.opencode-go]
                id = "opencode-go"
                base_url = "https://api.example.com"
            """),
            encoding="utf-8",
        )

        from go_aggregator.providers import connect as connect_module

        called: list[str] = []

        def fake_send_reload_signal(path: str) -> bool:
            called.append(path)
            return True

        monkeypatch.setattr(
            connect_module,
            "send_reload_signal",
            fake_send_reload_signal,
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "config", "refresh"],
        )

        assert result.exit_code == 0
        assert called == [str(config_path)]
        assert "Sent reload signal" in result.stdout

    def test_config_refresh_fails_when_no_router_running(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent("""\
                [providers.opencode-go]
                id = "opencode-go"
                base_url = "https://api.example.com"
            """),
            encoding="utf-8",
        )

        from go_aggregator.providers import connect as connect_module

        monkeypatch.setattr(connect_module, "send_reload_signal", lambda path: False)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "config", "refresh"],
        )

        assert result.exit_code == 1
        assert "No running server detected" in result.stderr
