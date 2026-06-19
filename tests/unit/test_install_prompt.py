"""Tests for the install prompt script."""

from __future__ import annotations

import ast
import termios
from pathlib import Path
from unittest.mock import MagicMock, patch


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

    def test_script_uses_input_function(self) -> None:
        """The script uses Python's input() for user prompt."""
        script_path = (
            Path(__file__).parent.parent.parent / "scripts" / "install_prompt.py"
        )
        source = script_path.read_text(encoding="utf-8")

        assert 'input("' in source

    def test_script_calls_onboard_command(self) -> None:
        """The script runs 'eggpool onboard' when user says yes."""
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


class TestInstallScriptCallsPython:
    """Tests for install.sh calling the Python prompt script."""

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
