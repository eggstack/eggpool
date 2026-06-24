"""Tests for the lifecycle uninstall module."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from eggpool.lifecycle.uninstall import (
    EGGPOOL_SHELL_MARKER,
    InstallMethod,
    UninstallPaths,
    _find_pipx_invocation,
    _pipx_uninstall,
    detect_install_method,
    pipx_uninstall,
    remove_eggpool_path_entries,
    resolve_uninstall_paths,
    uninstall,
    uv_tool_uninstall,
    verify_binary_removed,
    verify_eggpool_directory,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(root: Path) -> Path:
    """Create a minimal eggpool project under *root* with a pyproject.toml."""
    root.mkdir(parents=True, exist_ok=True)
    pyproject = root / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "eggpool"\nversion = "0.0.0"\n',
        encoding="utf-8",
    )
    (root / "src" / "eggpool").mkdir(parents=True)
    return root


def _fake_result(returncode: int = 0, stderr: str = "") -> MagicMock:
    """Build a subprocess.CompletedProcess-like fake."""
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stderr = stderr
    result.stdout = ""
    return result


# ---------------------------------------------------------------------------
# detect_install_method
# ---------------------------------------------------------------------------


class TestDetectInstallMethod:
    def test_detects_pipx_in_venv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """In a venv with pipx on PATH, returns pipx."""
        monkeypatch.setattr(sys, "base_prefix", "/usr")
        monkeypatch.setattr(sys, "prefix", "/some/venv")
        if hasattr(sys, "real_prefix"):
            monkeypatch.delattr(sys, "real_prefix")
        monkeypatch.setattr(
            "shutil.which", lambda name: "/usr/bin/pipx" if name == "pipx" else None
        )
        assert detect_install_method() == InstallMethod.PIPX

    def test_detects_uv_tool_in_uv_venv(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Executable path under ~/.local/share/uv/tools/ -> uv-tool."""
        monkeypatch.setattr(sys, "base_prefix", "/usr")
        monkeypatch.setattr(sys, "prefix", "/some/uv/venv")
        if hasattr(sys, "real_prefix"):
            monkeypatch.delattr(sys, "real_prefix")
        uv_exe = tmp_path / "uv" / "tools" / "eggpool" / "bin" / "python"
        uv_exe.parent.mkdir(parents=True)
        uv_exe.touch()
        monkeypatch.setattr(sys, "executable", str(uv_exe))
        monkeypatch.setattr("shutil.which", lambda _name: None)

        assert detect_install_method() == InstallMethod.UV_TOOL

    def test_detects_manual_in_venv_without_pipx(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In a venv with no pipx/uv markers, returns manual."""
        monkeypatch.setattr(sys, "base_prefix", "/usr")
        monkeypatch.setattr(sys, "prefix", "/some/venv")
        if hasattr(sys, "real_prefix"):
            monkeypatch.delattr(sys, "real_prefix")
        monkeypatch.setattr(sys, "executable", "/some/venv/bin/python")
        monkeypatch.setattr("shutil.which", lambda _name: None)
        assert detect_install_method() == InstallMethod.MANUAL

    def test_detects_source_checkout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Source install detected via pyproject.toml near __file__."""
        # Pretend we are NOT in a venv (point base_prefix at itself)
        # and ensure the real_prefix shim is gone.
        monkeypatch.setattr(sys, "base_prefix", sys.prefix)
        if hasattr(sys, "real_prefix"):
            monkeypatch.delattr(sys, "real_prefix")
        # The test runner is inside a real source checkout, so this
        # should resolve to SOURCE without further monkey-patching.
        assert detect_install_method() == InstallMethod.SOURCE


# ---------------------------------------------------------------------------
# verify_eggpool_directory
# ---------------------------------------------------------------------------


class TestVerifyEggpoolDirectory:
    def test_raises_when_no_candidate_matches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No pyproject.toml anywhere -> RuntimeError."""
        monkeypatch.setattr(sys, "executable", "/nonexistent/python")
        monkeypatch.setattr("pathlib.Path.cwd", lambda: Path("/nonexistent"))
        monkeypatch.setattr("pathlib.Path.home", lambda: Path("/nonexistent"))

        with pytest.raises(RuntimeError, match="Cannot verify"):
            verify_eggpool_directory()

    def test_returns_dir_when_pyproject_present(self, tmp_path: Path) -> None:
        """pyproject.toml with name=eggpool in CWD -> that dir."""
        project = _make_project(tmp_path)
        # Verify by pretending our source file is in the project too.
        # The function checks two levels up from THIS file, so we
        # intentionally construct a fake path under the project by
        # monkey-patching __file__'s parent structure.
        result = verify_eggpool_directory(cwd=project)
        assert result == project

    def test_finds_project_via_cwd(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path)
        result = verify_eggpool_directory(cwd=project)
        assert result == project

    def test_skips_dir_with_other_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pyproject.toml for a different project is not accepted."""
        other = tmp_path / "not-eggpool"
        other.mkdir()
        (other / "pyproject.toml").write_text(
            '[project]\nname = "other-project"\n',
            encoding="utf-8",
        )

        # Force the cwd and home to point at the unrelated project so
        # the source checkout (which legitimately contains an eggpool
        # pyproject) cannot satisfy the resolution.
        monkeypatch.setattr("pathlib.Path.cwd", lambda: other)
        monkeypatch.setattr("pathlib.Path.home", lambda: other)
        monkeypatch.setattr(sys, "executable", str(tmp_path / "nonexistent"))

        with pytest.raises(RuntimeError, match="Cannot verify"):
            verify_eggpool_directory()


# ---------------------------------------------------------------------------
# pipx_uninstall / uv_tool_uninstall (runner injection)
# ---------------------------------------------------------------------------


class TestPipxRunner:
    def test_pipx_invokes_correct_command(self) -> None:
        calls: list[dict[str, Any]] = []

        def runner(env: dict[str, str]) -> MagicMock:
            calls.append({"env": env})
            return _fake_result(returncode=0)

        pipx_uninstall(runner=runner, env={"FOO": "bar"})
        assert calls and calls[0]["env"] == {"FOO": "bar"}

    def test_uv_tool_invokes_correct_command(self) -> None:
        calls: list[dict[str, Any]] = []

        def runner(env: dict[str, str]) -> MagicMock:
            calls.append({"env": env})
            return _fake_result(returncode=0)

        uv_tool_uninstall(runner=runner, env={"FOO": "bar"})
        assert calls and calls[0]["env"] == {"FOO": "bar"}


# ---------------------------------------------------------------------------
# _find_pipx_invocation / _pipx_uninstall (subprocess argv selection)
# ---------------------------------------------------------------------------


class TestFindPipxInvocation:
    def test_prefers_bare_pipx_on_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Bare ``pipx`` on PATH wins over everything else."""
        uninstall_mod = sys.modules["eggpool.lifecycle.uninstall"]
        monkeypatch.setattr(
            "shutil.which",
            lambda name: "/usr/local/bin/pipx" if name == "pipx" else None,
        )
        monkeypatch.setattr(uninstall_mod, "_candidate_pythons", list)
        monkeypatch.setattr(uninstall_mod, "_python_has_pipx", lambda _p: True)
        monkeypatch.setattr(sys, "executable", "/some/venv/bin/python")

        assert _find_pipx_invocation(env={}) == [
            "/usr/local/bin/pipx",
            "uninstall",
            "eggpool",
        ]

    def test_uses_eggpool_python_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``EGGPOOL_PYTHON`` is honored when ``pipx`` is not on PATH."""
        override = tmp_path / "custom-python"
        override.write_text("", encoding="utf-8")
        override.chmod(0o755)

        monkeypatch.setattr("shutil.which", lambda _name: None)
        monkeypatch.setattr(sys, "executable", "/some/venv/bin/python")

        cmd = _find_pipx_invocation(env={"EGGPOOL_PYTHON": str(override)})

        assert cmd == [str(override), "-m", "pipx", "uninstall", "eggpool"]

    def test_falls_back_to_candidate_python_with_pipx(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Scans candidate Pythons and uses the first one with pipx."""
        uninstall_mod = sys.modules["eggpool.lifecycle.uninstall"]
        candidates = ["/usr/bin/python3", "/usr/local/bin/python3"]
        monkeypatch.setattr("shutil.which", lambda _name: None)
        monkeypatch.setattr(uninstall_mod, "_candidate_pythons", lambda: candidates)
        monkeypatch.setattr(
            uninstall_mod,
            "_python_has_pipx",
            lambda p: p == "/usr/local/bin/python3",
        )
        monkeypatch.setattr(sys, "executable", "/some/venv/bin/python")

        assert _find_pipx_invocation(env={}) == [
            "/usr/local/bin/python3",
            "-m",
            "pipx",
            "uninstall",
            "eggpool",
        ]

    def test_falls_back_to_sys_executable_when_nothing_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Last resort is ``sys.executable`` so pipx's own error surfaces."""
        uninstall_mod = sys.modules["eggpool.lifecycle.uninstall"]
        monkeypatch.setattr("shutil.which", lambda _name: None)
        monkeypatch.setattr(uninstall_mod, "_candidate_pythons", list)
        monkeypatch.setattr(uninstall_mod, "_python_has_pipx", lambda _p: False)
        monkeypatch.setattr(sys, "executable", "/some/venv/bin/python")

        assert _find_pipx_invocation(env={}) == [
            "/some/venv/bin/python",
            "-m",
            "pipx",
            "uninstall",
            "eggpool",
        ]


class TestPipxUninstallInvocation:
    def test_runs_bare_pipx_when_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_pipx_uninstall actually exec's the bare ``pipx`` command."""
        uninstall_mod = sys.modules["eggpool.lifecycle.uninstall"]
        monkeypatch.setattr(
            "shutil.which",
            lambda name: "/usr/local/bin/pipx" if name == "pipx" else None,
        )
        captured: dict[str, Any] = {}

        def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            return _fake_result(returncode=0)

        monkeypatch.setattr(uninstall_mod.subprocess, "run", fake_run)

        result = _pipx_uninstall(env={"FOO": "bar"})

        assert result.returncode == 0
        assert captured["cmd"] == ["/usr/local/bin/pipx", "uninstall", "eggpool"]
        assert captured["env"] == {"FOO": "bar"}

    def test_does_not_use_eggpool_venv_python_when_pipx_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Regression: the eggpool venv Python must not be invoked as pipx."""
        uninstall_mod = sys.modules["eggpool.lifecycle.uninstall"]
        # No bare pipx on PATH.
        monkeypatch.setattr("shutil.which", lambda _name: None)
        # No EGGPOOL_PYTHON override.
        # A candidate Python *does* have pipx installed.
        external = tmp_path / "system-python3"
        external.write_text("", encoding="utf-8")
        monkeypatch.setattr(
            uninstall_mod, "_candidate_pythons", lambda: [str(external)]
        )
        monkeypatch.setattr(uninstall_mod, "_python_has_pipx", lambda _p: True)
        # The eggpool venv Python (which is sys.executable).
        monkeypatch.setattr(sys, "executable", "/some/venv/bin/python")

        captured: dict[str, Any] = {}

        def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
            captured["cmd"] = cmd
            return _fake_result(returncode=0)

        monkeypatch.setattr(uninstall_mod.subprocess, "run", fake_run)

        _pipx_uninstall(env={})

        # Crucially: the eggpool venv Python is NOT used.
        assert captured["cmd"][0] != "/some/venv/bin/python"
        assert captured["cmd"] == [
            str(external),
            "-m",
            "pipx",
            "uninstall",
            "eggpool",
        ]


# ---------------------------------------------------------------------------
# remove_eggpool_path_entries
# ---------------------------------------------------------------------------


class TestRemoveEggpoolPathEntries:
    def test_removes_marked_block(self, tmp_path: Path) -> None:
        rc = tmp_path / ".zshrc"
        rc.write_text(
            "# top\n"
            "export PATH=/usr/bin:$PATH\n"
            f"{EGGPOOL_SHELL_MARKER}\n"
            'export PATH="$HOME/.local/bin:$PATH"\n'
            'export FOO="bar"\n'
            "\n"
            "# bottom\n"
        )

        modified = remove_eggpool_path_entries([rc])

        assert modified == [rc]
        text = rc.read_text(encoding="utf-8")
        assert EGGPOOL_SHELL_MARKER not in text
        assert "/usr/bin" in text  # unrelated line preserved
        # FOO lives inside the marker block (no blank between marker and
        # FOO), so it is removed as part of the block. That matches how
        # the install script writes its export group.
        assert "FOO" not in text
        assert "# bottom" in text

    def test_removes_pipx_specific_path(self, tmp_path: Path) -> None:
        rc = tmp_path / ".zshrc"
        rc.write_text(
            'export PATH="$HOME/.local/pipx/venvs/eggpool/bin:$PATH"\n'
            "export PATH=/usr/bin:$PATH\n"
        )

        modified = remove_eggpool_path_entries([rc])
        assert modified == [rc]
        text = rc.read_text(encoding="utf-8")
        assert "pipx/venvs/eggpool" not in text
        assert "/usr/bin" in text

    def test_removes_uv_tool_update_shell_line(self, tmp_path: Path) -> None:
        rc = tmp_path / ".zshrc"
        rc.write_text("uv tool update-shell\nexport PATH=/usr/bin:$PATH\n")

        modified = remove_eggpool_path_entries([rc])
        assert modified == [rc]
        text = rc.read_text(encoding="utf-8")
        assert "uv tool update-shell" not in text
        assert "/usr/bin" in text

    def test_preserves_generic_local_bin_line(self, tmp_path: Path) -> None:
        """Generic ~/.local/bin PATH line is NOT removed (might be other tools)."""
        rc = tmp_path / ".zshrc"
        rc.write_text('export PATH="$HOME/.local/bin:$PATH"\n')

        modified = remove_eggpool_path_entries([rc])
        assert modified == []

    def test_skips_missing_files(self, tmp_path: Path) -> None:
        rc = tmp_path / ".zshrc"
        rc.write_text("export PATH=/usr/bin:$PATH\n")
        ghost = tmp_path / "ghost"

        modified = remove_eggpool_path_entries([ghost, rc])
        assert modified == []

    def test_returns_empty_when_nothing_changed(self, tmp_path: Path) -> None:
        rc = tmp_path / ".zshrc"
        rc.write_text("export PATH=/usr/bin:$PATH\n")

        modified = remove_eggpool_path_entries([rc])
        assert modified == []


# ---------------------------------------------------------------------------
# verify_binary_removed
# ---------------------------------------------------------------------------


class TestVerifyBinaryRemoved:
    def test_returns_paths_when_still_present(self, tmp_path: Path) -> None:
        binary = tmp_path / "eggpool"
        binary.touch()

        def fake_which(name: str) -> str | None:
            return str(binary) if name == "eggpool" else None

        found = verify_binary_removed(which=fake_which)
        assert found == [binary.resolve()]

    def test_returns_empty_when_removed(self) -> None:
        found = verify_binary_removed(which=lambda _n: None)
        assert found == []

    def test_extra_paths_are_scanned(self, tmp_path: Path) -> None:
        binary = tmp_path / "eggpool"
        binary.touch()

        found = verify_binary_removed(
            which=lambda _n: None,
            extra_search_paths=[tmp_path],
        )
        assert found == [binary.resolve()]


# ---------------------------------------------------------------------------
# uninstall (top-level orchestrator)
# ---------------------------------------------------------------------------


def _make_paths(
    *,
    method: InstallMethod,
    config: Path,
    db: Path,
    binary: Path | None = None,
    eggpool_dir: Path | None = None,
    env_path: Path | None = None,
) -> UninstallPaths:
    return UninstallPaths(
        install_method=method,
        config_path=config,
        db_path=db,
        env_path=env_path,
        data_dir=db.parent,
        binary_path=binary,
        eggpool_dir=eggpool_dir,
    )


class TestUninstallPipx:
    def test_pipx_success_removes_config(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text("[server]\n")
        db = tmp_path / "usage.sqlite3"
        db.write_bytes(b"db")
        paths = _make_paths(method=InstallMethod.PIPX, config=config, db=db)

        runner = MagicMock(return_value=_fake_result(returncode=0))
        uninstall(
            paths=paths,
            confirm=lambda _m: True,
            cleanup_data=False,
            cleanup_config=True,
            cleanup_path=False,
            pipx_runner=runner,
        )

        runner.assert_called_once()
        assert not config.exists()

    def test_pipx_failure_raises(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text("[server]\n")
        db = tmp_path / "usage.sqlite3"
        db.write_bytes(b"db")
        paths = _make_paths(method=InstallMethod.PIPX, config=config, db=db)

        runner = MagicMock(return_value=_fake_result(returncode=1, stderr="boom"))
        with pytest.raises(RuntimeError, match="pipx uninstall failed"):
            uninstall(
                paths=paths,
                confirm=lambda _m: True,
                cleanup_data=False,
                cleanup_config=False,
                cleanup_path=False,
                pipx_runner=runner,
            )


class TestUninstallUvTool:
    def test_uv_tool_success(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text("[server]\n")
        db = tmp_path / "usage.sqlite3"
        db.write_bytes(b"db")
        paths = _make_paths(method=InstallMethod.UV_TOOL, config=config, db=db)

        runner = MagicMock(return_value=_fake_result(returncode=0))
        uninstall(
            paths=paths,
            confirm=lambda _m: True,
            cleanup_data=False,
            cleanup_config=False,
            cleanup_path=False,
            uv_tool_runner=runner,
        )
        runner.assert_called_once()

    def test_uv_tool_failure_raises(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text("[server]\n")
        db = tmp_path / "usage.sqlite3"
        db.write_bytes(b"db")
        paths = _make_paths(method=InstallMethod.UV_TOOL, config=config, db=db)

        runner = MagicMock(return_value=_fake_result(returncode=2))
        with pytest.raises(RuntimeError, match="uv tool uninstall failed"):
            uninstall(
                paths=paths,
                confirm=lambda _m: True,
                cleanup_data=False,
                cleanup_config=False,
                cleanup_path=False,
                uv_tool_runner=runner,
            )


class TestUninstallSource:
    def test_source_removes_project_dir(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path / "eggpool-source")
        config = project / "config.toml"
        config.write_text("[server]\n")
        db = project / "usage.sqlite3"
        db.write_bytes(b"db")
        paths = _make_paths(
            method=InstallMethod.SOURCE,
            config=config,
            db=db,
            eggpool_dir=project,
        )

        uninstall(
            paths=paths,
            confirm=lambda _m: True,
            cleanup_data=False,
            cleanup_config=False,
            cleanup_path=False,
        )

        assert not project.exists()

    def test_source_no_dir_raises(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text("[server]\n")
        db = tmp_path / "usage.sqlite3"
        db.write_bytes(b"db")
        paths = _make_paths(
            method=InstallMethod.SOURCE,
            config=config,
            db=db,
            eggpool_dir=None,
        )

        with pytest.raises(RuntimeError, match="no eggpool directory"):
            uninstall(
                paths=paths,
                confirm=lambda _m: True,
                cleanup_data=False,
                cleanup_config=False,
                cleanup_path=False,
            )


class TestUninstallManual:
    def test_manual_requires_binary(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text("[server]\n")
        db = tmp_path / "usage.sqlite3"
        db.write_bytes(b"db")
        paths = _make_paths(
            method=InstallMethod.MANUAL, config=config, db=db, binary=None
        )

        with pytest.raises(RuntimeError, match="Cannot locate"):
            uninstall(
                paths=paths,
                confirm=lambda _m: True,
                cleanup_data=False,
                cleanup_config=False,
                cleanup_path=False,
            )

    def test_manual_removes_binary(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text("[server]\n")
        db = tmp_path / "usage.sqlite3"
        db.write_bytes(b"db")
        binary = tmp_path / "eggpool"
        binary.touch()
        paths = _make_paths(
            method=InstallMethod.MANUAL, config=config, db=db, binary=binary
        )

        uninstall(
            paths=paths,
            confirm=lambda _m: True,
            cleanup_data=False,
            cleanup_config=False,
            cleanup_path=False,
        )

        assert not binary.exists()


class TestUninstallSideEffects:
    def test_cleanup_data_removes_data_dir(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "usage.sqlite3").write_bytes(b"db")
        config = tmp_path / "config.toml"
        config.write_text("[server]\n")
        paths = _make_paths(
            method=InstallMethod.PIPX,
            config=config,
            db=data_dir / "usage.sqlite3",
        )

        uninstall(
            paths=paths,
            confirm=lambda _m: True,
            cleanup_data=True,
            cleanup_config=False,
            cleanup_path=False,
            pipx_runner=MagicMock(return_value=_fake_result(0)),
        )

        assert not data_dir.exists()

    def test_keep_data_preserves_data_dir(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "usage.sqlite3").write_bytes(b"db")
        config = tmp_path / "config.toml"
        config.write_text("[server]\n")
        paths = _make_paths(
            method=InstallMethod.PIPX,
            config=config,
            db=data_dir / "usage.sqlite3",
        )

        uninstall(
            paths=paths,
            confirm=lambda _m: True,
            cleanup_data=False,
            cleanup_config=False,
            cleanup_path=False,
            pipx_runner=MagicMock(return_value=_fake_result(0)),
        )

        assert data_dir.exists()

    def test_cleanup_config_removes_env(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("API_KEY=x")
        config = tmp_path / "config.toml"
        config.write_text("[server]\n")
        db = tmp_path / "usage.sqlite3"
        db.write_bytes(b"db")
        paths = _make_paths(
            method=InstallMethod.PIPX,
            config=config,
            db=db,
            env_path=env,
        )

        uninstall(
            paths=paths,
            confirm=lambda _m: True,
            cleanup_data=False,
            cleanup_config=True,
            cleanup_path=False,
            pipx_runner=MagicMock(return_value=_fake_result(0)),
        )

        assert not env.exists()
        assert not config.exists()

    def test_cleanup_path_modifies_rc_files(self, tmp_path: Path) -> None:
        rc = tmp_path / ".zshrc"
        rc.write_text(f'{EGGPOOL_SHELL_MARKER}\nexport PATH="$HOME/.local/bin:$PATH"\n')
        config = tmp_path / "config.toml"
        config.write_text("[server]\n")
        db = tmp_path / "usage.sqlite3"
        db.write_bytes(b"db")
        paths = _make_paths(method=InstallMethod.PIPX, config=config, db=db)

        uninstall(
            paths=paths,
            confirm=lambda _m: True,
            cleanup_data=False,
            cleanup_config=False,
            cleanup_path=True,
            rc_files=[rc],
            pipx_runner=MagicMock(return_value=_fake_result(0)),
        )

        assert EGGPOOL_SHELL_MARKER not in rc.read_text(encoding="utf-8")

    def test_user_decline_aborts(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text("[server]\n")
        db = tmp_path / "usage.sqlite3"
        db.write_bytes(b"db")
        paths = _make_paths(method=InstallMethod.PIPX, config=config, db=db)

        # Decline the pipx question.
        responses = iter([False])

        with pytest.raises(RuntimeError, match="Uninstall aborted"):
            uninstall(
                paths=paths,
                confirm=lambda _m: next(responses),
                cleanup_data=False,
                cleanup_config=False,
                cleanup_path=False,
                pipx_runner=MagicMock(return_value=_fake_result(0)),
            )

        assert config.exists()


# ---------------------------------------------------------------------------
# resolve_uninstall_paths
# ---------------------------------------------------------------------------


class TestResolveUninstallPaths:
    def test_uses_default_db_when_config_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a config file, db_path is the XDG default."""
        config = tmp_path / "config.toml"
        monkeypatch.delenv("EGGPOOL_CONFIG", raising=False)

        paths = resolve_uninstall_paths(config, env={})

        from eggpool.constants import DEFAULT_DATABASE_PATH

        assert paths.db_path == Path(DEFAULT_DATABASE_PATH).expanduser().resolve()
        assert paths.config_path == config

    def test_reads_db_path_from_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When config has [database].path, that path is used."""
        config = tmp_path / "config.toml"
        config.write_text('[database]\npath = "/var/lib/eggpool/usage.sqlite3"\n')
        monkeypatch.delenv("EGGPOOL_CONFIG", raising=False)

        paths = resolve_uninstall_paths(config, env={})

        # Absolute paths are kept; expanduser/resolve may add the
        # macOS /private prefix but the trailing components match.
        assert str(paths.db_path).endswith("/var/lib/eggpool/usage.sqlite3")

    def test_detects_env_file_next_to_config(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        env = tmp_path / ".env"
        env.write_text("API_KEY=x")

        paths = resolve_uninstall_paths(config, env={})

        assert paths.env_path == env
