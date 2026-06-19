"""Tests for the provider connect module."""

from __future__ import annotations

import io
import os
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from eggpool.cli import cli
from eggpool.providers.connect import (
    ConfiguredAccount,
    TerminalMenu,
    _extract_raw_block,
    _format_provider_block,
    _provider_account_count,
    _toml_value,
    _unique_account_name,
    collect_api_key,
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
        assert len(templates) == 3  # alpha + beta + fallback opencode-go
        assert "alpha" in templates
        assert "beta" in templates
        assert "opencode-go" in templates

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
    async def test_fallback_when_file_missing(self, tmp_path: Path) -> None:
        """Falls back to opencode-go when file doesn't exist."""
        templates = load_provider_templates(str(tmp_path / "nonexistent.toml"))
        assert "opencode-go" in templates
        assert templates["opencode-go"]["url"] == "https://opencode.ai/zen/go/v1"

    @pytest.mark.asyncio()
    async def test_fallback_when_no_providers(self, tmp_path: Path) -> None:
        """Falls back to opencode-go when file has no [providers.*] sections."""
        providers_toml = tmp_path / "providers.toml"
        providers_toml.write_text('[server]\nhost = "localhost"\n')
        templates = load_provider_templates(str(providers_toml))
        assert "opencode-go" in templates
        assert templates["opencode-go"]["display"] == "OpenCode Go"


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


class TestUniqueAccountName:
    """Tests for generating unique account names."""

    def test_first_account(self) -> None:
        """First account gets '{provider_id}-0001'."""
        assert _unique_account_name("minimax", [], 0) == "minimax-0001"

    def test_second_account(self) -> None:
        """Second account gets '{provider_id}-0002'."""
        assert _unique_account_name("minimax", ["minimax-0001"], 1) == "minimax-0002"

    def test_third_account(self) -> None:
        """Third account gets '{provider_id}-0003'."""
        names = ["minimax-0001", "minimax-0002"]
        assert _unique_account_name("minimax", names, 2) == "minimax-0003"

    def test_preserves_existing_names(self) -> None:
        """Skips existing numbered names."""
        names = ["minimax-0001", "minimax-0002", "minimax-0004"]
        assert _unique_account_name("minimax", names, 3) == "minimax-0005"


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
        block = _format_provider_block(
            "my-provider", data, "MY_API_KEY", "my-provider-0001"
        )
        assert "[providers.my-provider]" in block
        assert 'base_url = "https://example.com"' in block
        assert 'protocols = ["openai"]' in block
        assert "[[providers.my-provider.accounts]]" in block
        assert 'api_key = "MY_API_KEY"' in block

    def test_non_default_fields(self) -> None:
        """Includes non-default fields like openai_path."""
        data = {
            "base_url": "https://example.com",
            "protocols": ["openai", "anthropic"],
            "openai_path": "/v1/chat/completions",
            "anthropic_path": "/anthropic/v1/messages",
        }
        block = _format_provider_block("test", data, "KEY", "test-0001")
        assert 'openai_path = "/v1/chat/completions"' in block
        assert 'anthropic_path = "/anthropic/v1/messages"' in block

    def test_excludes_accounts_and_api_key_env(self) -> None:
        """Does not include accounts or api_key_env in provider-level output."""
        data = {
            "id": "my-provider",
            "base_url": "https://example.com",
            "accounts": [],
            "api_key_env": "SHOULD_BE_EXCLUDED",
        }
        block = _format_provider_block(
            "my-provider", data, "MY_KEY", "my-provider-0001"
        )
        assert 'id = "my-provider"' in block
        assert "SHOULD_BE_EXCLUDED" not in block
        assert 'api_key = "SHOULD_BE_EXCLUDED"' not in block
        assert 'api_key = "MY_KEY"' in block

    def test_round_trip_with_app_config(self, tmp_path: Path) -> None:
        """Generated TOML can be parsed back by AppConfig.from_toml()."""
        from eggpool.models.config import AppConfig

        data = {
            "id": "minimax",
            "base_url": "https://api.minimaxi.com",
            "protocols": ["openai", "anthropic"],
            "openai_path": "/v1/chat/completions",
            "anthropic_path": "/anthropic/v1/messages",
            "models_path": "/v1/models",
            "api_key_env": "API_KEY",
        }
        block = _format_provider_block(
            "minimax", data, "MINIMAX_API_KEY", "minimax-0001"
        )

        config_file = tmp_path / "config.toml"
        config_file.write_text(block)
        config = AppConfig.from_toml(str(config_file))
        assert "minimax" in config.providers
        assert config.providers["minimax"].base_url == "https://api.minimaxi.com"
        assert config.providers["minimax"].id == "minimax"
        assert len(config.providers["minimax"].accounts) == 1
        assert config.providers["minimax"].accounts[0].api_key == "MINIMAX_API_KEY"


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
        assert 'api_key = "NEW_API_KEY"' in content

    def test_appends_account_to_existing_provider(self, tmp_path: Path) -> None:
        """Appends a new account to an existing provider."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            textwrap.dedent("""\
                [providers.minimax]
                id = "minimax"
                base_url = "https://api.minimaxi.com"

                [[providers.minimax.accounts]]
                name = "minimax-0001"
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
        assert 'name = "minimax-0002"' in content
        assert 'api_key = "MINIMAX_API_KEY_2"' in content

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
                name = "opencode-go-0001"
                api_key_env = "OPENCODE_GO_API_KEY"

                [[providers.opencode-go.accounts]]
                name = "opencode-go-0002"
                api_key_env = "OPENCODE_GO_API_KEY_2"
            """),
            encoding="utf-8",
        )

        ok = remove_account_from_config(
            str(config_file),
            ConfiguredAccount(
                provider_id="opencode-go",
                name="opencode-go-0001",
                api_key_env="OPENCODE_GO_API_KEY",
                api_key=None,
            ),
        )

        assert ok is True
        content = config_file.read_text(encoding="utf-8")
        assert "[providers.opencode-go]" in content
        assert 'name = "opencode-go-0001"' not in content
        assert 'name = "opencode-go-0002"' in content
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


class TestTerminalMenu:
    """Deterministic tests for TerminalMenu display and interactive navigation."""

    @pytest.fixture(autouse=True)
    def _mock_terminal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No-op the real terminal setup so tests run without a TTY."""
        monkeypatch.setattr(
            "eggpool.providers.connect.termios.tcgetattr", lambda _fd: None
        )
        monkeypatch.setattr(
            "eggpool.providers.connect.termios.tcsetattr", lambda *_a: None
        )
        monkeypatch.setattr("eggpool.providers.connect.tty.setraw", lambda _fd: None)

    @staticmethod
    def _pipe_stdin(input_bytes: bytes) -> io.TextIOWrapper:
        """Create a pipe-backed stdin from *input_bytes*.

        All bytes are written before the read end is exposed, so ``os.read``
        and ``select.select`` see them immediately.  This mirrors real TTY
        behaviour where escape sequences arrive as a single burst.
        """
        read_fd, write_fd = os.pipe()
        os.write(write_fd, input_bytes)
        os.close(write_fd)
        return io.TextIOWrapper(os.fdopen(read_fd, "rb", buffering=0))

    # -- navigation via os.read (the fixed code path) -----------------------

    def test_enter_selects_current(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pressing Enter returns the currently highlighted option."""
        monkeypatch.setattr("sys.stdin", self._pipe_stdin(b"\r"))
        assert TerminalMenu("T", ["A", "B"]).run() == "A"

    def test_quit_with_q(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pressing q returns None."""
        monkeypatch.setattr("sys.stdin", self._pipe_stdin(b"q"))
        assert TerminalMenu("T", ["A", "B"]).run() is None

    def test_quit_with_escape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A bare ESC (no following bytes) returns None."""
        monkeypatch.setattr("sys.stdin", self._pipe_stdin(b"\x1b"))
        assert TerminalMenu("T", ["A", "B"]).run() is None

    def test_arrow_down_then_enter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Down-arrow followed by Enter selects the second option."""
        monkeypatch.setattr("sys.stdin", self._pipe_stdin(b"\x1b[B\r"))
        assert TerminalMenu("T", ["A", "B", "C"]).run() == "B"

    def test_arrow_up_from_second(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Down then Up returns to the first option."""
        monkeypatch.setattr("sys.stdin", self._pipe_stdin(b"\x1b[B\x1b[A\r"))
        assert TerminalMenu("T", ["A", "B", "C"]).run() == "A"

    def test_multiple_down_arrows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Three down-arrows from the top select the last of four options."""
        inp = b"\x1b[B\x1b[B\x1b[B\r"
        monkeypatch.setattr("sys.stdin", self._pipe_stdin(inp))
        assert TerminalMenu("T", ["A", "B", "C", "D"]).run() == "D"

    def test_arrow_down_stops_at_bottom(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Extra down-arrows are clamped to the last option."""
        inp = b"\x1b[B\x1b[B\x1b[B\r"  # three downs on a two-item menu
        monkeypatch.setattr("sys.stdin", self._pipe_stdin(inp))
        assert TerminalMenu("T", ["A", "B"]).run() == "B"

    def test_arrow_up_stops_at_top(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Up-arrow at the first position stays on the first option."""
        monkeypatch.setattr("sys.stdin", self._pipe_stdin(b"\x1b[A\r"))
        assert TerminalMenu("T", ["A", "B"]).run() == "A"

    def test_j_k_navigation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """j/k keys navigate identically to arrow keys."""
        monkeypatch.setattr("sys.stdin", self._pipe_stdin(b"jjk\r"))
        assert TerminalMenu("T", ["A", "B", "C"]).run() == "B"

    def test_k_at_top_stays(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """k at the first position stays on the first option."""
        monkeypatch.setattr("sys.stdin", self._pipe_stdin(b"k\r"))
        assert TerminalMenu("T", ["A", "B"]).run() == "A"

    def test_j_at_bottom_stays(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """j at the last position stays on the last option."""
        monkeypatch.setattr("sys.stdin", self._pipe_stdin(b"jj\r"))
        assert TerminalMenu("T", ["A", "B"]).run() == "B"

    def test_eof_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty stdin (EOF) returns None immediately."""
        monkeypatch.setattr("sys.stdin", self._pipe_stdin(b""))
        assert TerminalMenu("T", ["A"]).run() is None

    # -- display output format -----------------------------------------------

    def test_display_uses_crnl(self, capsys: pytest.CaptureFixture[str]) -> None:
        """All line breaks in display output are \\r\\n (not bare \\n).

        In raw mode the kernel does not translate LF → CRLF, so the menu
        must emit explicit \\r\\n to avoid a cascading display.
        """
        menu = TerminalMenu("Pick:", ["Alpha", "Beta"])
        menu.display()
        output = capsys.readouterr().out

        # Every newline must be preceded by a carriage return
        for i, ch in enumerate(output):
            if ch == "\n":
                assert i > 0 and output[i - 1] == "\r", (
                    f"bare \\n at offset {i}; display must use \\r\\n in raw mode"
                )

    def test_display_lists_all_options(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Every option string appears in the rendered output."""
        menu = TerminalMenu("Pick:", ["Alpha", "Beta", "Gamma"])
        menu.display()
        output = capsys.readouterr().out

        assert "Alpha" in output
        assert "Beta" in output
        assert "Gamma" in output

    def test_display_selection_marker(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The selected option gets a green ``>`` marker; others do not."""
        menu = TerminalMenu("Pick:", ["Alpha", "Beta", "Gamma"])
        menu.selected = 1
        menu.display()
        output = capsys.readouterr().out

        # Each option line is separated by \r\n
        lines = output.split("\r\n")
        alpha_lines = [line for line in lines if "Alpha" in line]
        beta_lines = [line for line in lines if "Beta" in line]

        assert alpha_lines, "Alpha option missing from display"
        assert beta_lines, "Beta option missing from display"

        # Beta (selected) has "> " and green color
        assert "> " in beta_lines[0]
        assert "\033[1;32m" in beta_lines[0]

        # Alpha (not selected) has neither "> " nor green color
        assert "> " not in alpha_lines[0]
        assert "\033[1;32m" not in alpha_lines[0]

    def test_display_title_and_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The title and navigation help appear in the output."""
        menu = TerminalMenu("Pick a provider:", ["A"])
        menu.display()
        output = capsys.readouterr().out

        assert "Pick a provider:" in output
        assert "j/k" in output
        assert "Enter" in output
        assert "q/Esc" in output

    def test_display_clears_screen(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Output starts with the clear-screen + cursor-home sequence."""
        menu = TerminalMenu("T", ["A"])
        menu.display()
        output = capsys.readouterr().out

        assert output.startswith("\033[2J\033[H")


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

        from eggpool.providers import connect as connect_module

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


def test_config_refresh_command_removed() -> None:
    """Live config reload is unsupported; the CLI must not expose it."""
    runner = CliRunner()
    result = runner.invoke(cli, ["config", "refresh"])

    assert result.exit_code != 0
    assert "No such command" in result.stderr


class TestEditCommand:
    """Tests for ``eggpool edit`` command."""

    def test_edit_opens_config_in_editor(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Calls os.execvp with the editor and config path."""
        import os as os_mod

        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\nport = 8080\n")

        exec_args: list[list[str]] = []

        def fake_execvp(cmd: str, args: list[str]) -> None:
            exec_args.append(args)

        monkeypatch.setattr(os_mod, "execvp", fake_execvp)
        monkeypatch.setenv("EDITOR", "vim")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "edit"],
        )

        assert result.exit_code == 0
        assert len(exec_args) == 1
        assert exec_args[0] == ["vim", str(config_path)]

    def test_edit_falls_back_to_available_editor(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Falls back to an available editor when no EDITOR/VISUAL is set."""
        import os as os_mod

        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\nport = 8080\n")

        exec_args: list[list[str]] = []

        def fake_execvp(cmd: str, args: list[str]) -> None:
            exec_args.append(args)

        monkeypatch.setattr(os_mod, "execvp", fake_execvp)
        monkeypatch.delenv("EDITOR", raising=False)
        monkeypatch.delenv("VISUAL", raising=False)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "edit"],
        )

        assert result.exit_code == 0
        assert len(exec_args) == 1
        assert exec_args[0][1] == str(config_path)
        # Should be one of the known fallback editors
        assert exec_args[0][0] in ("hx", "vim", "vi", "nano")


class TestProviderFallback:
    """Tests for the hardcoded opencode-go fallback."""

    def test_fallback_always_present(self, tmp_path: Path) -> None:
        """opencode-go appears even when the file has other providers."""
        providers_toml = tmp_path / "providers.toml"
        providers_toml.write_text(
            textwrap.dedent("""\
                [providers.other]
                id = "other"
                base_url = "https://other.example.com"
                protocols = ["openai"]
                api_key_env = "API_KEY"
            """)
        )
        templates = load_provider_templates(str(providers_toml))
        assert "other" in templates
        assert "opencode-go" in templates

    def test_fallback_not_duplicated_when_in_file(self, tmp_path: Path) -> None:
        """opencode-go from file is used, not the hardcoded fallback."""
        providers_toml = tmp_path / "providers.toml"
        providers_toml.write_text(
            textwrap.dedent("""\
                [providers.opencode-go]
                id = "opencode-go"
                base_url = "https://custom.example.com"
                protocols = ["openai"]
                api_key_env = "API_KEY"
            """)
        )
        templates = load_provider_templates(str(providers_toml))
        assert templates["opencode-go"]["url"] == "https://custom.example.com"

    def test_fallback_has_required_keys(self) -> None:
        """Fallback template has all required keys."""
        from eggpool.providers.connect import _OPENCODE_GO_FALLBACK

        for _id, tmpl in _OPENCODE_GO_FALLBACK.items():
            assert "display" in tmpl
            assert "url" in tmpl
            assert "raw" in tmpl
            assert "data" in tmpl
            assert "id" in tmpl["data"]


class TestCollectApiKey:
    """Tests for collect_api_key with mocked stdin."""

    @pytest.fixture(autouse=True)
    def _mock_terminal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No-op the real terminal setup so tests run without a TTY."""
        monkeypatch.setattr(
            "eggpool.providers.connect.termios.tcgetattr", lambda _fd: None
        )
        monkeypatch.setattr(
            "eggpool.providers.connect.termios.tcsetattr", lambda *_a: None
        )
        monkeypatch.setattr("eggpool.providers.connect.tty.setraw", lambda _fd: None)

    @staticmethod
    def _pipe_stdin(input_bytes: bytes) -> io.TextIOWrapper:
        """Create a pipe-backed stdin from *input_bytes*."""
        read_fd, write_fd = os.pipe()
        os.write(write_fd, input_bytes)
        os.close(write_fd)
        return io.TextIOWrapper(os.fdopen(read_fd, "rb", buffering=0))

    def test_enter_with_empty_input(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pressing Enter immediately returns empty string."""
        monkeypatch.setattr("sys.stdin", self._pipe_stdin(b"\r"))
        assert collect_api_key("Test") == ""

    def test_typing_key_and_enter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Typed characters are captured and returned."""
        monkeypatch.setattr("sys.stdin", self._pipe_stdin(b"sk-test-123\r"))
        assert collect_api_key("Test") == "sk-test-123"

    def test_esc_cancels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pressing Esc returns empty string."""
        monkeypatch.setattr("sys.stdin", self._pipe_stdin(b"\x1b"))
        assert collect_api_key("Test") == ""

    def test_eof_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty stdin (EOF) returns empty string."""
        monkeypatch.setattr("sys.stdin", self._pipe_stdin(b""))
        assert collect_api_key("Test") == ""

    def test_backspace_removes_char(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Backspace removes the last typed character."""
        monkeypatch.setattr("sys.stdin", self._pipe_stdin(b"ab\x7fc\r"))
        assert collect_api_key("Test") == "ac"

    def test_arrow_key_does_not_add_char(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Arrow key escape sequences are discarded, not added to input."""
        monkeypatch.setattr("sys.stdin", self._pipe_stdin(b"\x1b[Ax\r"))
        assert collect_api_key("Test") == "x"

    def test_ctrl_c_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ctrl+C raises KeyboardInterrupt."""
        monkeypatch.setattr("sys.stdin", self._pipe_stdin(b"\x03"))
        with pytest.raises(KeyboardInterrupt):
            collect_api_key("Test")


class TestNoCancelledOutput:
    """Tests that Esc/q menu exits produce no output."""

    def test_connect_silent_on_esc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Connect prints nothing when the user presses Esc in the menu."""
        from eggpool.providers import connect as connect_module

        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent("""\
                [server]
                port = 8080
            """),
            encoding="utf-8",
        )
        providers_path = tmp_path / "providers.toml"
        providers_path.write_text(
            textwrap.dedent("""\
                [providers.alpha]
                id = "alpha"
                base_url = "https://alpha.example.com"
                protocols = ["openai"]
                api_key_env = "API_KEY"
            """),
            encoding="utf-8",
        )

        class EscMenu:
            """Menu that simulates pressing Esc."""

            def __init__(self, _title: str, _options: list[str]) -> None:
                pass

            def run(self) -> None:
                return None

        monkeypatch.setattr(connect_module, "TerminalMenu", EscMenu)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_path),
                "connect",
                "--providers",
                str(providers_path),
            ],
        )

        assert result.exit_code == 1
        assert "Cancelled" not in result.output

    def test_logout_silent_on_esc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Logout prints nothing when the user presses Esc in the menu."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent("""\
                [providers.alpha]
                id = "alpha"
                base_url = "https://alpha.example.com"

                [[providers.alpha.accounts]]
                name = "default"
                api_key_env = "ALPHA_KEY"

                [[providers.alpha.accounts]]
                name = "second"
                api_key_env = "ALPHA_KEY_2"
            """),
            encoding="utf-8",
        )
        monkeypatch.setenv("ALPHA_KEY", "key1")
        monkeypatch.setenv("ALPHA_KEY_2", "key2")

        from eggpool.providers import connect as connect_module

        class EscMenu:
            """Menu that simulates pressing Esc."""

            def __init__(self, _title: str, _options: list[str]) -> None:
                pass

            def run(self) -> None:
                return None

        monkeypatch.setattr(connect_module, "TerminalMenu", EscMenu)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "logout", "alpha"],
        )

        assert result.exit_code == 0
        assert "Cancelled" not in result.output


class TestUpdateCommand:
    """Tests for the ``update`` CLI command."""

    def test_check_only_up_to_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--check reports up to date when versions match."""
        import importlib.metadata

        monkeypatch.setattr(
            importlib.metadata,
            "version",
            lambda _name: "1.2.3",
        )

        import httpx

        class FakeResponse:
            def __init__(self) -> None:
                self.status_code = 200

            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict[str, str]:
                return {"tag_name": "v1.2.3"}

        monkeypatch.setattr(httpx, "get", lambda _url, **_kw: FakeResponse())

        runner = CliRunner()
        result = runner.invoke(cli, ["update", "--check"])

        assert result.exit_code == 0
        assert "Already up to date" in result.output

    def test_check_only_update_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--check reports available when versions differ."""
        import importlib.metadata

        monkeypatch.setattr(
            importlib.metadata,
            "version",
            lambda _name: "1.2.3",
        )

        import httpx

        class FakeResponse:
            def __init__(self) -> None:
                self.status_code = 200

            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict[str, str]:
                return {"tag_name": "v1.2.4"}

        monkeypatch.setattr(httpx, "get", lambda _url, **_kw: FakeResponse())

        runner = CliRunner()
        result = runner.invoke(cli, ["update", "--check"])

        assert result.exit_code == 0
        assert "An update is available" in result.output

    def test_update_installs_and_restarts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Full update path installs and restarts the server."""
        import importlib.metadata
        import subprocess

        call_log: list[list[str]] = []

        def fake_version(_name: str) -> str:
            return "1.2.3"

        monkeypatch.setattr(importlib.metadata, "version", fake_version)

        import httpx

        class FakeResponse:
            def __init__(self) -> None:
                self.status_code = 200

            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict[str, str]:
                return {"tag_name": "v1.2.4"}

        monkeypatch.setattr(httpx, "get", lambda _url, **_kw: FakeResponse())

        def fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            call_log.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        from eggpool.providers import connect as connect_module

        monkeypatch.setattr(connect_module, "signal_restart", lambda: True)

        runner = CliRunner()
        result = runner.invoke(cli, ["update"])

        assert result.exit_code == 0
        assert "Updating from 1.2.3 to 1.2.4" in result.output
        assert "Server restarted." in result.output
        assert len(call_log) == 1
        assert "pip" in call_log[0][0] or "-m" in call_log[0][1]

    def test_update_github_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exits with error when GitHub API fails."""
        import importlib.metadata

        monkeypatch.setattr(
            importlib.metadata,
            "version",
            lambda _name: "1.2.3",
        )

        import httpx

        def fake_get(_url: str, **_kw: object) -> None:
            raise httpx.HTTPError("network error")

        monkeypatch.setattr(httpx, "get", fake_get)

        runner = CliRunner()
        result = runner.invoke(cli, ["update"])

        assert result.exit_code == 1
        assert "Error checking for updates" in result.output


class TestConfigAccountLabel:
    """Tests for ConfiguredAccount.label format."""

    def test_label_with_api_key(self) -> None:
        """Shows masked key when api_key is set."""
        acct = ConfiguredAccount(
            provider_id="opencode-go",
            name="default",
            api_key_env="OPENCODE_GO_API_KEY",
            api_key="sk-live-key-1234",
        )
        assert acct.label == "opencode-go/default  sk-l...1234"

    def test_label_with_env_only(self) -> None:
        """Shows env var name when only api_key_env is set."""
        acct = ConfiguredAccount(
            provider_id="opencode-go",
            name="default",
            api_key_env="OPENCODE_GO_API_KEY",
            api_key=None,
        )
        assert acct.label == "opencode-go/default  env:OPENCODE_GO_API_KEY"

    def test_label_unset(self) -> None:
        """Shows 'unset' when neither key nor env is available."""
        acct = ConfiguredAccount(
            provider_id="opencode-go",
            name="default",
            api_key_env="",
            api_key=None,
        )
        assert acct.label == "opencode-go/default  unset"


class TestListConfigAccounts:
    """Tests for list_config_accounts and select_config_account."""

    def test_list_config_accounts_returns_all(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Returns all accounts from the config file."""
        from eggpool.providers.connect import list_config_accounts

        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent("""\
                [providers.opencode-go]
                id = "opencode-go"
                base_url = "https://api.example.com"

                [[providers.opencode-go.accounts]]
                name = "default"
                api_key = "sk-key-1"

                [[providers.opencode-go.accounts]]
                name = "second"
                api_key = "sk-key-2"

                [providers.minimax]
                id = "minimax"
                base_url = "https://minimax.example.com"

                [[providers.minimax.accounts]]
                name = "minimax-default"
                api_key_env = "MINIMAX_KEY"
            """),
            encoding="utf-8",
        )
        monkeypatch.setenv("MINIMAX_KEY", "sk-minimax")

        accts = list_config_accounts(str(config_path))
        assert len(accts) == 3
        assert accts[0].provider_id == "opencode-go"
        assert accts[0].name == "default"
        assert accts[0].api_key == "sk-key-1"
        assert accts[1].name == "second"
        assert accts[2].provider_id == "minimax"
        assert accts[2].name == "minimax-default"
        assert accts[2].api_key == "sk-minimax"

    def test_list_config_accounts_empty(self, tmp_path: Path) -> None:
        """Returns empty list when no providers configured."""
        from eggpool.providers.connect import list_config_accounts

        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\nport = 8080\n")

        assert list_config_accounts(str(config_path)) == []

    def test_select_config_account_returns_selected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Returns the account matching the menu selection."""
        from eggpool.providers import connect as connect_module
        from eggpool.providers.connect import select_config_account

        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent("""\
                [providers.opencode-go]
                id = "opencode-go"
                base_url = "https://api.example.com"

                [[providers.opencode-go.accounts]]
                name = "default"
                api_key = "sk-key-1"

                [[providers.opencode-go.accounts]]
                name = "second"
                api_key = "sk-key-2"
            """),
            encoding="utf-8",
        )

        class FakeMenu:
            def __init__(self, title: str, options: list[str]) -> None:
                self.options = options

            def run(self) -> str:
                return self.options[1]

        monkeypatch.setattr(connect_module, "TerminalMenu", FakeMenu)

        acct = select_config_account(str(config_path))
        assert acct is not None
        assert acct.name == "second"

    def test_select_config_account_returns_none_on_quit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Returns None when user quits the menu."""
        from eggpool.providers import connect as connect_module
        from eggpool.providers.connect import select_config_account

        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent("""\
                [providers.opencode-go]
                id = "opencode-go"
                base_url = "https://api.example.com"

                [[providers.opencode-go.accounts]]
                name = "default"
                api_key = "sk-key-1"
            """),
            encoding="utf-8",
        )

        class FakeMenu:
            def __init__(self, title: str, options: list[str]) -> None:
                pass

            def run(self) -> None:
                return None

        monkeypatch.setattr(connect_module, "TerminalMenu", FakeMenu)

        acct = select_config_account(str(config_path))
        assert acct is None

    def test_select_config_account_empty_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Returns None and prints message when no accounts exist."""
        from eggpool.providers.connect import select_config_account

        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\nport = 8080\n")

        acct = select_config_account(str(config_path))
        assert acct is None


class TestAccountsListCli:
    """Tests for ``eggpool accounts list`` CLI command."""

    def test_accounts_list_shows_configured(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Lists all configured accounts with labels."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent("""\
                [providers.opencode-go]
                id = "opencode-go"
                base_url = "https://api.example.com"

                [[providers.opencode-go.accounts]]
                name = "default"
                api_key = "sk-key-1"
            """),
            encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "accounts", "list"],
        )

        assert result.exit_code == 0
        assert "opencode-go/default" in result.output
        assert "Total: 1 accounts" in result.output

    def test_accounts_list_empty(self, tmp_path: Path) -> None:
        """Shows helpful message when no accounts configured."""
        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\nport = 8080\n")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", str(config_path), "accounts", "list"],
        )

        assert result.exit_code == 0
        assert "No configured accounts" in result.output


class TestLogoutWithoutTarget:
    """Tests for ``logout`` without a target argument (interactive selection)."""

    def test_logout_no_target_shows_menu(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without a target, shows selection menu and removes selected account."""
        from eggpool.providers import connect as connect_module
        from eggpool.providers.connect import (
            remove_account_from_config,
            select_config_account,
        )

        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent("""\
                [providers.opencode-go]
                id = "opencode-go"
                base_url = "https://api.example.com"

                [[providers.opencode-go.accounts]]
                name = "default"
                api_key = "sk-key-1"

                [[providers.opencode-go.accounts]]
                name = "second"
                api_key = "sk-key-2"
            """),
            encoding="utf-8",
        )

        class FakeMenu:
            def __init__(self, title: str, options: list[str]) -> None:
                self.options = options

            def run(self) -> str:
                return self.options[0]

        monkeypatch.setattr(connect_module, "TerminalMenu", FakeMenu)

        account = select_config_account(str(config_path))
        assert account is not None
        assert account.name == "default"

        ok = remove_account_from_config(str(config_path), account)
        assert ok is True
        content = config_path.read_text(encoding="utf-8")
        assert 'name = "default"' not in content
        assert 'name = "second"' in content

    def test_logout_no_target_quit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Quitting the menu removes nothing."""
        from eggpool.providers import connect as connect_module
        from eggpool.providers.connect import select_config_account

        config_path = tmp_path / "config.toml"
        config_path.write_text(
            textwrap.dedent("""\
                [providers.opencode-go]
                id = "opencode-go"
                base_url = "https://api.example.com"

                [[providers.opencode-go.accounts]]
                name = "default"
                api_key = "sk-key-1"
            """),
            encoding="utf-8",
        )

        class FakeMenu:
            def __init__(self, title: str, options: list[str]) -> None:
                pass

            def run(self) -> None:
                return None

        monkeypatch.setattr(connect_module, "TerminalMenu", FakeMenu)

        account = select_config_account(str(config_path))
        assert account is None

        content = config_path.read_text(encoding="utf-8")
        assert 'name = "default"' in content
