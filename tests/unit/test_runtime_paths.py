"""Unit tests for :mod:`eggpool.runtime_paths`."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

from eggpool.runtime_paths import (
    clear_pid_file,
    default_log_file,
    default_pid_file,
    is_process_running,
    read_pid_file,
    state_dir,
)


def test_state_dir_under_local_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    result = state_dir()
    assert result == tmp_path / ".local" / "state" / "eggpool"


def test_state_dir_creates_missing_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    result = state_dir()
    assert result.is_dir()


def test_default_pid_file_honors_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pid_file = tmp_path / "explicit.pid"
    monkeypatch.setenv("EGGPOOL_PID_FILE", str(pid_file))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert default_pid_file() == pid_file


def test_default_pid_file_honors_xdg_runtime_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("EGGPOOL_PID_FILE", raising=False)
    xdg = tmp_path / "xdg-runtime"
    xdg.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
    assert default_pid_file() == xdg / "eggpool.pid"


def test_default_pid_file_falls_back_to_state_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("EGGPOOL_PID_FILE", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert (
        default_pid_file() == tmp_path / ".local" / "state" / "eggpool" / "eggpool.pid"
    )


def test_default_pid_file_uid_scoped_tmp_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("EGGPOOL_PID_FILE", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def _fake_state_dir() -> Path:
        path = tmp_path / ".local" / "state" / "eggpool"
        with contextlib.suppress(OSError):
            path.mkdir(parents=True, exist_ok=True)
        return path

    monkeypatch.setattr("eggpool.runtime_paths.state_dir", _fake_state_dir)

    from unittest.mock import patch as mock_patch

    real_exists = Path.exists

    def _exists_must_be_false(self: Path) -> bool:
        if str(self).endswith(".local/state/eggpool"):
            return False
        return real_exists(self)

    with mock_patch.object(Path, "exists", _exists_must_be_false):
        result = default_pid_file()
    assert result == Path("/tmp") / f"eggpool-{os.getuid()}.pid"


def test_default_log_file_honors_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_path = tmp_path / "explicit.log"
    monkeypatch.setenv("EGGPOOL_LOG_FILE", str(log_path))
    assert default_log_file() == log_path


def test_default_log_file_falls_back_to_state_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("EGGPOOL_LOG_FILE", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert (
        default_log_file() == tmp_path / ".local" / "state" / "eggpool" / "eggpool.log"
    )


def test_read_pid_file_returns_none_for_missing(tmp_path: Path) -> None:
    assert read_pid_file(tmp_path / "missing.pid") is None


def test_read_pid_file_returns_none_for_invalid(tmp_path: Path) -> None:
    bad = tmp_path / "bad.pid"
    bad.write_text("not-a-number", encoding="utf-8")
    assert read_pid_file(bad) is None


def test_read_pid_file_returns_int_when_valid(tmp_path: Path) -> None:
    pid_file = tmp_path / "ok.pid"
    pid_file.write_text("12345\n", encoding="utf-8")
    assert read_pid_file(pid_file) == 12345


def test_is_process_running_true_for_current_pid() -> None:
    assert is_process_running(os.getpid()) is True


def test_is_process_running_false_for_dead_pid() -> None:
    assert is_process_running(os.getpid() + 10**8) is False


def test_clear_pid_file_removes_existing(tmp_path: Path) -> None:
    pid_file = tmp_path / "eggpool.pid"
    pid_file.write_text("12345", encoding="utf-8")
    clear_pid_file(pid_file)
    assert not pid_file.exists()


def test_clear_pid_file_silent_on_missing(tmp_path: Path) -> None:
    clear_pid_file(tmp_path / "never-existed.pid")
