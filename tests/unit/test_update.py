"""Tests for the update CLI command and install method detection."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from eggpool.cli import _detect_install_method, cli

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invoke_update(
    args: list[str] | None = None,
    *,
    current: str = "0.1.0",
    latest: str = "0.2.0",
    method: str = "pip",
    run_returncode: int = 0,
    run_stderr: str = "",
    pypi_error: str | None = None,
) -> tuple[int, str, list[list[str]] | None]:
    """Invoke ``eggpool update`` with fully mocked dependencies.

    Returns (exit_code, output, list_of_subprocess_commands_or_None).
    """
    call_log: list[list[str]] | None = None

    if pypi_error is not None:
        check_result = (current, "", pypi_error)
    else:
        check_result = (current, latest, "")

    runner = CliRunner()
    with (
        patch(
            "eggpool.cli_full.async_check_for_update",
            return_value=check_result,
        ),
        patch("eggpool.cli_full._detect_install_method", return_value=method),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=run_returncode, stderr=run_stderr)
        from eggpool.providers import connect as connect_mod

        with patch.object(connect_mod, "restart_server", return_value=False):
            result = runner.invoke(cli, ["update", *(args or [])])

    if mock_run.called:
        call_log = [mock_run.call_args[0][0]]

    return result.exit_code, result.output, call_log


# ---------------------------------------------------------------------------
# _detect_install_method
# ---------------------------------------------------------------------------


class TestDetectInstallMethod:
    """Unit tests for _detect_install_method()."""

    def test_returns_source_in_source_checkout(self) -> None:
        """Returns 'source' when not in a venv and pyproject.toml is nearby."""
        # In the test runner we ARE in a source checkout, so when we
        # ensure we're not detected as "in a venv", it should find
        # the source checkout.
        with (
            patch.object(sys, "base_prefix", sys.prefix),
            patch("shutil.which", return_value=None),
        ):
            # Ensure real_prefix doesn't exist (it doesn't in Python 3)
            had = hasattr(sys, "real_prefix")
            if had:
                delattr(sys, "real_prefix")
            try:
                result = _detect_install_method()
                # Should detect source since we're in a source checkout
                assert result == "source"
            finally:
                if had:
                    sys.real_prefix = type(sys.prefix)()  # type: ignore[attr-defined]

    def test_venv_with_pipx_returns_pipx(self) -> None:
        """Returns 'pipx' when eggpool binary path is under pipx/venvs."""
        with (
            patch.object(sys, "base_prefix", "/different/prefix"),
            patch(
                "shutil.which",
                return_value="/home/user/.local/pipx/venvs/eggpool/bin/eggpool",
            ),
        ):
            had = hasattr(sys, "real_prefix")
            if had:
                delattr(sys, "real_prefix")
            try:
                result = _detect_install_method()
                assert result == "pipx"
            finally:
                if had:
                    sys.real_prefix = type(sys.prefix)()  # type: ignore[attr-defined]

    def test_venv_uv_tool_path_returns_uv_tool(self, tmp_path: Path) -> None:
        """Returns 'uv-tool' when executable path has 'uv' and 'tools'."""
        uv_exe = str(tmp_path / "uv" / "tools" / "eggpool" / "bin" / "python")
        with (
            patch.object(sys, "base_prefix", "/different/prefix"),
            patch.object(sys, "executable", uv_exe),
            patch("shutil.which", return_value=None),
        ):
            had = hasattr(sys, "real_prefix")
            if had:
                delattr(sys, "real_prefix")
            try:
                result = _detect_install_method()
                assert result == "uv-tool"
            finally:
                if had:
                    sys.real_prefix = type(sys.prefix)()  # type: ignore[attr-defined]

    def test_venv_no_pipx_no_uv_returns_pip(self) -> None:
        """Returns 'pip' when in a venv but not pipx or uv-tool."""
        with (
            patch.object(sys, "base_prefix", "/different/prefix"),
            patch.object(sys, "executable", "/some/venv/bin/python"),
            patch("shutil.which", return_value=None),
        ):
            had = hasattr(sys, "real_prefix")
            if had:
                delattr(sys, "real_prefix")
            try:
                result = _detect_install_method()
                assert result == "pip"
            finally:
                if had:
                    sys.real_prefix = type(sys.prefix)()  # type: ignore[attr-defined]

    def test_pipx_priority_over_generic_venv(self) -> None:
        """Returns 'pipx' even if executable path doesn't match uv-tool."""
        with (
            patch.object(sys, "base_prefix", "/different/prefix"),
            patch.object(
                sys, "executable", "/home/user/.local/pipx/venvs/eggpool/bin/python"
            ),
            patch(
                "shutil.which",
                return_value="/home/user/.local/pipx/venvs/eggpool/bin/eggpool",
            ),
        ):
            had = hasattr(sys, "real_prefix")
            if had:
                delattr(sys, "real_prefix")
            try:
                result = _detect_install_method()
                assert result == "pipx"
            finally:
                if had:
                    sys.real_prefix = type(sys.prefix)()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# update --check
# ---------------------------------------------------------------------------


class TestUpdateCheckOnly:
    def test_reports_available(self) -> None:
        """--check reports available when versions differ."""
        code, output, _ = _invoke_update(
            args=["--check"], current="0.1.0", latest="0.2.0"
        )
        assert code == 0
        assert "Current version: 0.1.0" in output
        assert "Latest version:  0.2.0" in output
        assert "An update is available." in output

    def test_reports_up_to_date(self) -> None:
        """--check reports up to date when versions match."""
        code, output, _ = _invoke_update(
            args=["--check"], current="0.1.0", latest="0.1.0"
        )
        assert code == 0
        assert "Already up to date." in output

    def test_no_install_run(self) -> None:
        """--check never runs subprocess."""
        code, _, calls = _invoke_update(
            args=["--check"], current="0.1.0", latest="0.2.0"
        )
        assert code == 0
        assert calls is None


# ---------------------------------------------------------------------------
# update per install method
# ---------------------------------------------------------------------------


class TestUpdateByInstallMethod:
    def test_pip_install(self) -> None:
        """pip method runs pip install --upgrade."""
        code, output, calls = _invoke_update(method="pip")
        assert code == 0
        assert "Updating from 0.1.0 to 0.2.0" in output
        assert calls is not None
        cmd = calls[0]
        assert cmd[-1] == "eggpool"
        assert "--upgrade" in cmd

    def test_pipx_install(self) -> None:
        """pipx method runs pipx upgrade."""
        code, output, calls = _invoke_update(method="pipx")
        assert code == 0
        assert calls is not None
        assert calls[0] == ["pipx", "upgrade", "eggpool"]

    def test_uv_tool_install(self) -> None:
        """uv-tool method runs uv tool install with pinned version."""
        code, output, calls = _invoke_update(method="uv-tool")
        assert code == 0
        assert calls is not None
        assert calls[0] == ["uv", "tool", "install", "eggpool==0.2.0"]

    def test_source_install(self) -> None:
        """source method runs uv sync --no-dev with --directory."""
        code, output, calls = _invoke_update(method="source")
        assert code == 0
        assert calls is not None
        cmd = calls[0]
        assert cmd[:3] == ["uv", "sync", "--no-dev"]
        assert "--directory" in cmd


# ---------------------------------------------------------------------------
# update --from-source per install method
# ---------------------------------------------------------------------------


class TestUpdateFromSource:
    def test_from_source_on_pip(self) -> None:
        """--from-source on pip install uses pip install git+https://."""
        code, output, calls = _invoke_update(args=["--from-source"], method="pip")
        assert code == 0
        assert calls is not None
        cmd = calls[0]
        joined = " ".join(cmd)
        assert "git+https://github.com/eggstack/eggpool.git@v0.2.0" in joined
        assert "pip" in cmd[0] or cmd[0] == sys.executable

    def test_from_source_on_pipx(self) -> None:
        """--from-source on pipx install uses pipx install git+https://."""
        code, output, calls = _invoke_update(args=["--from-source"], method="pipx")
        assert code == 0
        assert calls is not None
        cmd = calls[0]
        assert cmd[0] == "pipx"
        assert cmd[1] == "install"
        assert "git+https://github.com/eggstack/eggpool.git@v0.2.0" in cmd[2]

    def test_from_source_on_uv_tool(self) -> None:
        """--from-source on uv-tool uses uv tool install git+https://."""
        code, output, calls = _invoke_update(args=["--from-source"], method="uv-tool")
        assert code == 0
        assert calls is not None
        cmd = calls[0]
        assert cmd[0] == "uv"
        assert cmd[1] == "tool"
        assert cmd[2] == "install"
        assert "git+https://github.com/eggstack/eggpool.git@v0.2.0" in cmd[3]

    def test_from_source_on_source(self) -> None:
        """--from-source on source install uses uv sync --no-dev."""
        code, output, calls = _invoke_update(args=["--from-source"], method="source")
        assert code == 0
        assert calls is not None
        assert calls[0] == ["uv", "sync", "--no-dev"]


# ---------------------------------------------------------------------------
# update failure paths
# ---------------------------------------------------------------------------


class TestUpdateFailures:
    def test_pypi_error(self) -> None:
        """Exits 1 when PyPI request fails."""
        code, output, _ = _invoke_update(pypi_error="pypi: network error")
        assert code == 1
        assert "Error checking for updates" in output
        assert "network error" in output

    def test_subprocess_failure(self) -> None:
        """Exits 1 when subprocess returns non-zero."""
        code, output, _ = _invoke_update(
            method="pip", run_returncode=1, run_stderr="install failed"
        )
        assert code == 1
        assert "Update failed" in output
        assert "install failed" in output

    def test_empty_pypi_version(self) -> None:
        """Exits 1 when PyPI returns empty version."""
        code, output, _ = _invoke_update(latest="")
        assert code == 1
        assert "Could not determine latest version" in output


# ---------------------------------------------------------------------------
# install script
# ---------------------------------------------------------------------------


class TestInstallScript:
    def test_pipx_path_is_mutually_exclusive_with_uv(self) -> None:
        """pipx and uv paths in install.sh are in if/else, not sequential."""
        script = Path(__file__).parent.parent.parent / "scripts" / "install.sh"
        source = script.read_text()

        pipx_idx = source.index("pipx --version")
        else_idx = source.index("else", pipx_idx)
        uv_idx = source.index("uv package manager")
        assert pipx_idx < else_idx < uv_idx

    def test_install_script_recommends_onboard(self) -> None:
        """install.sh recommends 'eggpool onboard' not 'init-config'."""
        script = Path(__file__).parent.parent.parent / "scripts" / "install.sh"
        source = script.read_text()
        assert "eggpool onboard" in source
        assert "eggpool init-config" not in source
