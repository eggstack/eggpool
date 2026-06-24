"""Tests for the install prompt script."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestFindEggpoolDir:
    """Tests for _find_eggpool_dir helper."""

    def test_finds_dir_from_script_location(self, tmp_path: Path) -> None:
        """Finds eggpool dir when pyproject.toml exists relative to script."""
        repo_dir = tmp_path / "eggpool"
        repo_dir.mkdir()
        scripts_dir = repo_dir / "scripts"
        scripts_dir.mkdir()
        (repo_dir / "pyproject.toml").write_text('name = "eggpool"\n')
        script_path = scripts_dir / "install_prompt.py"
        script_path.write_text("# placeholder")

        with patch("scripts.install_prompt.__file__", str(script_path)):
            from scripts.install_prompt import _find_eggpool_dir

            result = _find_eggpool_dir()

        assert isinstance(result, str)

    def test_falls_back_to_eggpool_home(self, tmp_path: Path) -> None:
        """Falls back to ~/eggpool when script location doesn't have pyproject."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        eggpool_dir = fake_home / "eggpool"
        eggpool_dir.mkdir()
        (eggpool_dir / "pyproject.toml").write_text('name = "eggpool"\n')

        with (
            patch("scripts.install_prompt.os.path.dirname") as mock_dirname,
            patch("scripts.install_prompt.os.path.expanduser") as mock_expand,
            patch("scripts.install_prompt.os.path.isfile") as mock_isfile,
        ):
            mock_dirname.side_effect = [
                str(tmp_path / "other"),
                str(tmp_path / "other"),
            ]
            mock_expand.return_value = str(eggpool_dir)
            mock_isfile.side_effect = lambda p: "pyproject.toml" in str(p)

            from scripts.install_prompt import _find_eggpool_dir

            with patch("scripts.install_prompt.open", MagicMock()):
                result = _find_eggpool_dir()

        assert isinstance(result, str)


class TestResolveEggpoolCmd:
    """Tests for _resolve_eggpool_cmd — picks bare vs uv-run fallback."""

    def test_prefers_bare_eggpool_when_on_path(self) -> None:
        """When `eggpool` resolves on PATH, use it bare with --config."""
        from scripts.install_prompt import _resolve_eggpool_cmd

        with patch("scripts.install_prompt.shutil.which") as mock_which:
            mock_which.side_effect = lambda name: (
                "/usr/local/bin/eggpool" if name == "eggpool" else None
            )
            cmd, mode = _resolve_eggpool_cmd("/tmp/cfg.toml")

        assert cmd == ["eggpool", "--config", "/tmp/cfg.toml"]
        assert mode == "global"

    def test_falls_back_to_uv_run_when_eggpool_missing(self, tmp_path: Path) -> None:
        """When `eggpool` is not on PATH, fall back to `uv run` in the repo dir."""
        from scripts.install_prompt import _resolve_eggpool_cmd

        repo_dir = tmp_path / "eggpool"
        repo_dir.mkdir()
        (repo_dir / "pyproject.toml").write_text('name = "eggpool"\n')

        with (
            patch("scripts.install_prompt.shutil.which") as mock_which,
            patch(
                "scripts.install_prompt._find_eggpool_dir", return_value=str(repo_dir)
            ),
        ):
            mock_which.side_effect = lambda name: (
                "/usr/local/bin/uv" if name == "uv" else None
            )
            cmd, mode = _resolve_eggpool_cmd(str(repo_dir / "config.toml"))

        assert mode == "uv-run"
        assert cmd[0] == "uv"
        assert cmd[1] == "run"
        assert "--directory" in cmd
        assert "eggpool" in cmd
        assert cmd[-2:] == ["--config", str(repo_dir / "config.toml")]

    def test_raises_when_neither_eggpool_nor_uv_available(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No `eggpool` and no `uv` -> SystemExit with actionable stderr message."""
        from scripts.install_prompt import _resolve_eggpool_cmd

        with (
            patch("scripts.install_prompt.shutil.which", return_value=None),
            patch(
                "scripts.install_prompt._find_eggpool_dir", return_value=str(tmp_path)
            ),
            pytest.raises(SystemExit) as exit_info,
        ):
            _resolve_eggpool_cmd(str(tmp_path / "config.toml"))

        assert exit_info.value.code == 1
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "eggpool" in combined
        assert "uv" in combined
        assert "PATH" in combined

    def test_uv_run_fallback_requires_pyproject(self, tmp_path: Path) -> None:
        """uv-run fallback is rejected when no pyproject.toml is present."""
        from scripts.install_prompt import _resolve_eggpool_cmd

        with (
            patch("scripts.install_prompt.shutil.which") as mock_which,
            patch(
                "scripts.install_prompt._find_eggpool_dir", return_value=str(tmp_path)
            ),
            pytest.raises(SystemExit),
        ):
            mock_which.side_effect = lambda name: (
                "/usr/local/bin/uv" if name == "uv" else None
            )
            _resolve_eggpool_cmd(str(tmp_path / "config.toml"))


class TestInstallPromptAST:
    """AST-based tests to verify install_prompt.py structure."""

    def test_script_has_main_function(self) -> None:
        """The script defines a main() function."""
        script_path = (
            Path(__file__).parent.parent.parent / "scripts" / "install_prompt.py"
        )
        source = script_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        functions = [
            node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        ]
        assert "main" in functions

    def test_script_has_find_eggpool_dir(self) -> None:
        """The script defines _find_eggpool_dir()."""
        script_path = (
            Path(__file__).parent.parent.parent / "scripts" / "install_prompt.py"
        )
        source = script_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        functions = [
            node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        ]
        assert "_find_eggpool_dir" in functions

    def test_script_has_resolve_eggpool_cmd(self) -> None:
        """The script defines _resolve_eggpool_cmd()."""
        script_path = (
            Path(__file__).parent.parent.parent / "scripts" / "install_prompt.py"
        )
        source = script_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        functions = [
            node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        ]
        assert "_resolve_eggpool_cmd" in functions

    def test_script_uses_input_function(self) -> None:
        """The script uses Python's input() for user prompt."""
        script_path = (
            Path(__file__).parent.parent.parent / "scripts" / "install_prompt.py"
        )
        source = script_path.read_text(encoding="utf-8")

        assert 'input("' in source

    def test_script_calls_onboard_command(self) -> None:
        """The script runs the onboard command when user says yes."""
        script_path = (
            Path(__file__).parent.parent.parent / "scripts" / "install_prompt.py"
        )
        source = script_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        found_onboard = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "onboard" in node.value
            ):
                found_onboard = True
                break

        assert found_onboard, "Script should reference the onboard command"

    def test_script_passes_config_flag(self) -> None:
        """The script passes --config to the eggpool invocation."""
        script_path = (
            Path(__file__).parent.parent.parent / "scripts" / "install_prompt.py"
        )
        source = script_path.read_text(encoding="utf-8")

        assert '"--config"' in source
        assert "config_path" in source

    def test_script_has_if_name_main(self) -> None:
        """The script runs main() when executed directly."""
        script_path = (
            Path(__file__).parent.parent.parent / "scripts" / "install_prompt.py"
        )
        source = script_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        found_main_guard = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare)
                and any(
                    isinstance(c, ast.Constant) and c.value == "__main__"
                    for c in [node.test.left, *node.test.comparators]
                )
            ):
                found_main_guard = True
                break

        assert found_main_guard, "Script should have if __name__ == '__main__' guard"

    def test_script_has_no_termios_import(self) -> None:
        """The script does not import termios (uses input() instead)."""
        script_path = (
            Path(__file__).parent.parent.parent / "scripts" / "install_prompt.py"
        )
        source = script_path.read_text(encoding="utf-8")

        assert "import termios" not in source


class TestInstallPromptBehavior:
    """Tests for install_prompt.py behavior."""

    def test_yes_runs_bare_eggpool_when_on_path(self) -> None:
        """Answering 'y' invokes the bare `eggpool` command with --config."""
        from scripts.install_prompt import main

        eggpool_dir = "/tmp/eggpool"
        with (
            patch(
                "scripts.install_prompt._find_eggpool_dir",
                return_value=eggpool_dir,
            ),
            patch(
                "scripts.install_prompt.shutil.which",
                side_effect=lambda name: (
                    "/usr/local/bin/eggpool" if name == "eggpool" else None
                ),
            ),
            patch("builtins.input", return_value="y"),
            patch("scripts.install_prompt.subprocess.run") as mock_run,
            pytest.raises(SystemExit) as exit_info,
        ):
            mock_run.return_value.returncode = 0
            main()

        mock_run.assert_called_once()
        actual_cmd = mock_run.call_args.args[0]
        assert actual_cmd[0] == "eggpool"
        assert "--config" in actual_cmd
        assert f"{eggpool_dir}/config.toml" in actual_cmd
        assert actual_cmd[-1] == "onboard"
        assert exit_info.value.code == 0

    def test_yes_falls_back_to_uv_run_when_eggpool_missing(
        self, tmp_path: Path
    ) -> None:
        """Answering 'y' falls back to `uv run eggpool` when bare cmd unavailable."""
        from scripts.install_prompt import main

        repo_dir = tmp_path / "eggpool"
        repo_dir.mkdir()
        (repo_dir / "pyproject.toml").write_text('name = "eggpool"\n')

        with (
            patch(
                "scripts.install_prompt._find_eggpool_dir",
                return_value=str(repo_dir),
            ),
            patch("scripts.install_prompt.shutil.which") as mock_which,
            patch("builtins.input", return_value="y"),
            patch("scripts.install_prompt.subprocess.run") as mock_run,
            pytest.raises(SystemExit) as exit_info,
        ):
            mock_which.side_effect = lambda name: (
                "/usr/local/bin/uv" if name == "uv" else None
            )
            mock_run.return_value.returncode = 0
            main()

        mock_run.assert_called_once()
        actual_cmd = mock_run.call_args.args[0]
        assert actual_cmd[:3] == ["uv", "run", "--directory"]
        assert "eggpool" in actual_cmd
        assert "--config" in actual_cmd
        assert exit_info.value.code == 0

    def test_yes_exits_helpfully_when_neither_eggpool_nor_uv(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No `eggpool`, no `uv` -> SystemExit with remediation message."""
        from scripts.install_prompt import main

        repo_dir = tmp_path / "eggpool"
        repo_dir.mkdir()
        (repo_dir / "pyproject.toml").write_text('name = "eggpool"\n')

        with (
            patch(
                "scripts.install_prompt._find_eggpool_dir",
                return_value=str(repo_dir),
            ),
            patch("scripts.install_prompt.shutil.which", return_value=None),
            patch("builtins.input", return_value="y"),
            pytest.raises(SystemExit) as exit_info,
        ):
            main()

        assert exit_info.value.code != 0
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "eggpool" in combined
        assert "PATH" in combined

    def test_eof_on_input_shows_skip_message(self, tmp_path: Path) -> None:
        """EOF on input gracefully shows skip message instead of crashing."""
        import subprocess

        script_path = (
            Path(__file__).parent.parent.parent / "scripts" / "install_prompt.py"
        )
        repo_dir = tmp_path / "eggpool"
        repo_dir.mkdir()
        (repo_dir / "pyproject.toml").write_text('name = "eggpool"\n')

        # Run with empty stdin to trigger EOFError
        result = subprocess.run(  # noqa: S603
            ["python3", str(script_path)],
            cwd=str(repo_dir),
            input="",
            capture_output=True,
            text=True,
            timeout=5,
        )

        assert result.returncode == 0
        assert "Skipping onboarding" in result.stdout

    def test_no_shows_skip_message(self, tmp_path: Path) -> None:
        """Answering 'n' shows skip message."""
        import subprocess

        script_path = (
            Path(__file__).parent.parent.parent / "scripts" / "install_prompt.py"
        )
        repo_dir = tmp_path / "eggpool"
        repo_dir.mkdir()
        (repo_dir / "pyproject.toml").write_text('name = "eggpool"\n')

        result = subprocess.run(  # noqa: S603
            ["python3", str(script_path)],
            cwd=str(repo_dir),
            input="n\n",
            capture_output=True,
            text=True,
            timeout=5,
        )

        assert result.returncode == 0
        assert "Skipping onboarding" in result.stdout

    def test_install_script_references_prompt_script(self) -> None:
        """install.sh calls install_prompt.py."""
        install_path = Path(__file__).parent.parent.parent / "scripts" / "install.sh"
        source = install_path.read_text(encoding="utf-8")

        assert "install_prompt.py" in source

    def test_install_script_does_not_use_read_for_onboarding(self) -> None:
        """install.sh does not use 'read -r ONBOARD_CHOICE' for onboarding."""
        install_path = Path(__file__).parent.parent.parent / "scripts" / "install.sh"
        source = install_path.read_text(encoding="utf-8")

        assert "read -r ONBOARD_CHOICE" not in source

    def test_install_script_reconnects_prompt_to_terminal(self) -> None:
        """A curl-piped install reads the prompt from its controlling terminal."""
        install_path = Path(__file__).parent.parent.parent / "scripts" / "install.sh"
        source = install_path.read_text(encoding="utf-8")

        assert "exec 3</dev/tty" in source
        assert 'install_prompt.py" <&3' in source

    def test_install_script_uses_uv_tool_install_in_fallback(self) -> None:
        """install.sh's no-pipx fallback uses uv tool install."""
        install_path = Path(__file__).parent.parent.parent / "scripts" / "install.sh"
        source = install_path.read_text(encoding="utf-8")

        assert "uv tool install" in source
        assert "uv tool update-shell" in source

    def test_install_script_fallback_prints_bare_eggpool_commands(self) -> None:
        """install.sh's fallback path prints commands using bare `eggpool`."""
        install_path = Path(__file__).parent.parent.parent / "scripts" / "install.sh"
        source = install_path.read_text(encoding="utf-8")

        assert "eggpool --config" in source
        assert "uv run eggpool accounts" not in source

    def test_install_script_uses_eggpool_version_not_dashdash(self) -> None:
        """install.sh uses `eggpool version`, not `eggpool --version`."""
        install_path = Path(__file__).parent.parent.parent / "scripts" / "install.sh"
        source = install_path.read_text(encoding="utf-8")

        assert "eggpool version" in source
        assert "eggpool --version" not in source

    def test_install_script_has_no_incorrect_dashdash_flags(self) -> None:
        """install.sh does not use -- on any top-level eggpool subcommand."""
        install_path = Path(__file__).parent.parent.parent / "scripts" / "install.sh"
        source = install_path.read_text(encoding="utf-8")

        # Top-level eggpool subcommands (not options) that should never
        # be invoked with a leading --.  --config is a valid option;
        # --help is handled by Click natively; --since is a journalctl
        # flag and does not appear in install.sh.
        bad_subcommands = [
            "eggpool --version",
            "eggpool --accounts",
            "eggpool --connect",
            "eggpool --deploy",
            "eggpool --onboard",
            "eggpool --serve",
            "eggpool --migrate",
            "eggpool --help",
            "eggpool --stop",
            "eggpool --restart",
            "eggpool --newkey",
            "eggpool --rehash",
            "eggpool --croncheck",
        ]
        for bad in bad_subcommands:
            assert bad not in source, f"install.sh contains incorrect invocation: {bad}"

    def test_install_script_bash_syntax(self) -> None:
        """install.sh parses without bash syntax errors."""
        import subprocess

        install_path = Path(__file__).parent.parent.parent / "scripts" / "install.sh"
        result = subprocess.run(  # noqa: S603
            ["bash", "-n", str(install_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"install.sh has bash syntax errors:\n{result.stderr}"
        )

    def test_install_prompt_py_syntax(self) -> None:
        """install_prompt.py parses without Python syntax errors."""
        import subprocess

        script_path = (
            Path(__file__).parent.parent.parent / "scripts" / "install_prompt.py"
        )
        result = subprocess.run(  # noqa: S603
            ["python3", "-c", f"import ast; ast.parse(open('{script_path}').read())"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"install_prompt.py has syntax errors:\n{result.stderr}"
        )
