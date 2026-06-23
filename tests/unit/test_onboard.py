"""Tests for onboarding, stop, and restart CLI commands."""

from __future__ import annotations

import ast
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from eggpool.cli import _is_process_running, _read_pid, _wait_for_exit, cli


class TestReadPid:
    """Tests for _read_pid helper."""

    def test_returns_none_when_no_pid_file(self, tmp_path: Path) -> None:
        """Returns None when PID file does not exist."""
        with patch("eggpool.constants.PID_FILE", tmp_path / "nonexistent.pid"):
            assert _read_pid() is None

    def test_returns_pid_when_valid(self, tmp_path: Path) -> None:
        """Returns PID when file contains valid integer."""
        pid_file = tmp_path / "eggpool.pid"
        pid_file.write_text("12345")
        with patch("eggpool.constants.PID_FILE", pid_file):
            assert _read_pid() == 12345

    def test_returns_none_when_invalid_content(self, tmp_path: Path) -> None:
        """Returns None when file contains non-integer content."""
        pid_file = tmp_path / "eggpool.pid"
        pid_file.write_text("not-a-pid")
        with patch("eggpool.constants.PID_FILE", pid_file):
            assert _read_pid() is None

    def test_returns_none_when_empty_file(self, tmp_path: Path) -> None:
        """Returns None when file is empty."""
        pid_file = tmp_path / "eggpool.pid"
        pid_file.write_text("")
        with patch("eggpool.constants.PID_FILE", pid_file):
            assert _read_pid() is None


class TestIsProcessRunning:
    """Tests for _is_process_running helper."""

    def test_current_process_is_running(self) -> None:
        """The current process is always running."""
        assert _is_process_running(os.getpid()) is True

    def test_nonexistent_process(self) -> None:
        """A very high PID is unlikely to exist."""
        # Use a PID that's almost certainly not running
        assert _is_process_running(999999999) is False


class TestWaitForExit:
    """Tests for _wait_for_exit helper."""

    def test_returns_true_for_nonexistent_process(self) -> None:
        """Returns True immediately for a process that doesn't exist."""
        # Process doesn't exist, so _is_process_running returns False immediately
        assert _wait_for_exit(999999999, timeout=0.1) is True


class TestStopCommand:
    """Tests for the stop CLI command."""

    def test_stop_when_not_running(self, tmp_path: Path) -> None:
        """Stop reports server is not running when no PID file."""
        runner = CliRunner()
        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\n")

        with patch("eggpool.constants.PID_FILE", tmp_path / "nonexistent.pid"):
            result = runner.invoke(cli, ["--config", str(config_path), "stop"])

        assert result.exit_code == 0
        assert "Server is not running" in result.output

    def test_stop_when_stale_pid(self, tmp_path: Path) -> None:
        """Stop cleans up stale PID file."""
        pid_file = tmp_path / "eggpool.pid"
        pid_file.write_text("999999999")  # Non-existent PID

        runner = CliRunner()
        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\n")

        with patch("eggpool.constants.PID_FILE", pid_file):
            result = runner.invoke(cli, ["--config", str(config_path), "stop"])

        assert result.exit_code == 0
        assert "not running (stale PID file)" in result.output
        assert not pid_file.exists()


class TestRestartCommand:
    """Tests for the restart CLI command."""

    def test_restart_starts_server(self, tmp_path: Path) -> None:
        """Restart starts a new server process."""
        runner = CliRunner()
        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\n")

        with (
            patch("eggpool.constants.PID_FILE", tmp_path / "nonexistent.pid"),
            patch("subprocess.Popen") as mock_popen,
        ):
            mock_popen.return_value = MagicMock()
            result = runner.invoke(cli, ["--config", str(config_path), "restart"])

        assert result.exit_code == 0
        assert "Starting server" in result.output
        assert "Server started" in result.output
        mock_popen.assert_called_once()


class TestOnboardCommand:
    """Tests for the onboard CLI command."""

    def test_onboard_cli_exists(self) -> None:
        """The onboard command is registered in the CLI."""
        runner = CliRunner()
        result = runner.invoke(cli, ["onboard", "--help"])
        assert result.exit_code == 0
        assert "onboarding" in result.output.lower()

    def test_onboard_exits_after_single_connect(self, tmp_path: Path) -> None:
        """Onboard exits after a single connect when user declines to add another."""
        from unittest.mock import MagicMock, patch

        import eggpool.onboard as onboard_mod

        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\n")

        with (
            patch("eggpool.providers.connect.connect", return_value=True),
            patch.object(onboard_mod, "_prompt_add_another", return_value=False),
            patch("subprocess.run", return_value=MagicMock(returncode=0)),
            patch("os.execvp"),
        ):
            onboard_mod.run_onboarding(str(config_path), "providers.toml")


class TestOnboardFreshInstall:
    """Tests for fresh-install onboarding behavior."""

    def test_ensure_config_creates_minimal_config(self, tmp_path: Path) -> None:
        """_ensure_config_with_api_key creates config if missing."""
        from eggpool.onboard import _ensure_config_with_api_key

        config_path = str(tmp_path / "config.toml")
        _ensure_config_with_api_key(config_path)

        assert Path(config_path).exists()
        content = Path(config_path).read_text()
        assert "[server]" in content
        assert "[database]" in content
        assert "[models]" in content

    def test_ensure_config_generates_api_key(self, tmp_path: Path) -> None:
        """_ensure_config_with_api_key generates server API key if missing."""
        from eggpool.onboard import _ensure_config_with_api_key

        config_path = str(tmp_path / "config.toml")
        _ensure_config_with_api_key(config_path)

        import tomllib

        with open(config_path, "rb") as f:
            config = tomllib.load(f)

        api_key = config.get("server", {}).get("api_key", "")
        assert api_key.startswith("ep_")
        assert len(api_key) > 10

    def test_ensure_config_preserves_existing_api_key(self, tmp_path: Path) -> None:
        """_ensure_config_with_api_key does not overwrite existing API key."""
        from eggpool.onboard import _ensure_config_with_api_key

        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[server]\napi_key = "ep_existing_key_12345"\nport = 11300\n'
        )

        _ensure_config_with_api_key(str(config_path))

        import tomllib

        with open(config_path, "rb") as f:
            config = tomllib.load(f)

        assert config["server"]["api_key"] == "ep_existing_key_12345"

    def test_fresh_install_onboard_creates_config_and_key(self, tmp_path: Path) -> None:
        """Full onboard flow creates config and API key on fresh install."""
        from unittest.mock import MagicMock, patch

        import eggpool.onboard as onboard_mod

        config_path = str(tmp_path / "config.toml")

        with (
            patch("eggpool.providers.connect.connect", return_value=True),
            patch.object(onboard_mod, "_prompt_add_another", return_value=False),
            patch("subprocess.run", return_value=MagicMock(returncode=0)),
            patch("os.execvp"),
        ):
            onboard_mod.run_onboarding(config_path, "providers.toml")

        assert Path(config_path).exists()

        import tomllib

        with open(config_path, "rb") as f:
            config = tomllib.load(f)

        # Config has server section with API key
        assert config.get("server", {}).get("api_key", "").startswith("ep_")
        # Config has required sections
        assert "database" in config
        assert "models" in config

    def test_install_script_recommends_onboard(self) -> None:
        """install.sh recommends 'eggpool onboard' not 'init-config'."""
        install_path = Path(__file__).parent.parent.parent / "scripts" / "install.sh"
        source = install_path.read_text(encoding="utf-8")

        assert "eggpool onboard" in source
        assert "eggpool init-config" not in source


class TestOnboardPromptFunctions:
    """Tests for onboarding prompt functions."""

    def test_prompt_yn_imports_correctly(self) -> None:
        """_prompt_yn is importable from onboard module."""
        from eggpool.onboard import _prompt_yn

        assert callable(_prompt_yn)

    def test_prompt_add_another_imports_correctly(self) -> None:
        """_prompt_add_another is importable from onboard module."""
        from eggpool.onboard import _prompt_add_another

        assert callable(_prompt_add_another)


class TestStopRestartAST:
    """AST-based tests to verify stop and restart command structure."""

    def test_stop_command_uses_signal_term(self) -> None:
        """The stop command sends SIGTERM (not SIGKILL)."""
        cli_path = Path(__file__).parent.parent.parent / "src" / "eggpool" / "cli.py"
        source = cli_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        found_sigterm = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "SIGTERM":
                found_sigterm = True
                break

        assert found_sigterm, "stop/restart commands should use SIGTERM"

    def test_restart_uses_popen(self) -> None:
        """The restart command uses subprocess.Popen to start new server."""
        cli_path = Path(__file__).parent.parent.parent / "src" / "eggpool" / "cli.py"
        source = cli_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        found_popen = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "Popen":
                found_popen = True
                break

        assert found_popen, "restart command should use subprocess.Popen"

    def test_stop_does_not_use_os_exec(self) -> None:
        """The stop command does not use os.exec (that's for serve)."""
        cli_path = Path(__file__).parent.parent.parent / "src" / "eggpool" / "cli.py"
        source = cli_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # Find the stop function and check it doesn't use os.exec
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "stop":
                for child in ast.walk(node):
                    if isinstance(child, ast.Attribute) and child.attr == "exec":
                        pytest.fail("stop command should not use os.exec")
