"""Unit tests for :mod:`eggpool.fastcli`."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from eggpool import fastcli


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["croncheck"], (None, "croncheck")),
        (["ensure-running"], (None, "ensure-running")),
        (["--config", "foo.toml", "croncheck"], ("foo.toml", "croncheck")),
        (["croncheck", "--config", "foo.toml"], ("foo.toml", "croncheck")),
        (["--config=foo.toml", "croncheck"], ("foo.toml", "croncheck")),
        (["-c", "foo.toml", "croncheck"], ("foo.toml", "croncheck")),
        (["serve"], (None, None)),
        (["serve", "--config", "foo.toml"], ("foo.toml", None)),
        (["--help"], (None, None)),
        (["--unknown-flag", "value", "croncheck"], (None, "croncheck")),
        (["croncheck", "--unknown-flag", "value"], (None, "croncheck")),
    ],
)
def test_parse_simple_argv(
    argv: list[str], expected: tuple[str | None, str | None]
) -> None:
    assert fastcli._parse_simple_argv(argv) == expected


def test_maybe_run_fast_command_returns_none_for_serve() -> None:
    assert fastcli.maybe_run_fast_command(["serve"]) is None


def test_maybe_run_fast_command_returns_none_for_help() -> None:
    assert fastcli.maybe_run_fast_command(["--help"]) is None


def test_maybe_run_fast_command_croncheck_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "eggpool.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr("eggpool.fastcli.default_pid_file", lambda: pid_file)
    assert fastcli.maybe_run_fast_command(["croncheck"]) == 0


def test_maybe_run_fast_command_croncheck_missing_pid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "missing.pid"
    monkeypatch.setattr("eggpool.fastcli.default_pid_file", lambda: pid_file)
    assert fastcli.maybe_run_fast_command(["croncheck"]) == 1


def test_maybe_run_fast_command_croncheck_invalid_pid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "bad.pid"
    pid_file.write_text("not-a-number", encoding="utf-8")
    monkeypatch.setattr("eggpool.fastcli.default_pid_file", lambda: pid_file)
    assert fastcli.maybe_run_fast_command(["croncheck"]) == 1


def test_maybe_run_fast_command_croncheck_dead_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "dead.pid"
    pid_file.write_text(str(os.getpid() + 10**8), encoding="utf-8")
    monkeypatch.setattr("eggpool.fastcli.default_pid_file", lambda: pid_file)
    assert fastcli.maybe_run_fast_command(["croncheck"]) == 1


def test_ensure_running_does_not_spawn_when_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "alive.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr("eggpool.fastcli.default_pid_file", lambda: pid_file)

    with patch("eggpool.fastcli.subprocess.Popen") as mock_popen:
        result = fastcli._run_ensure_running(None)

    assert result == 0
    mock_popen.assert_not_called()


def test_ensure_running_spawns_when_pid_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "missing.pid"
    monkeypatch.setattr("eggpool.fastcli.default_pid_file", lambda: pid_file)

    mock_proc = MagicMock()
    with patch(
        "eggpool.fastcli.subprocess.Popen", return_value=mock_proc
    ) as mock_popen:
        result = fastcli._run_ensure_running(None)

    assert result == 0
    mock_popen.assert_called_once()
    argv = mock_popen.call_args[0][0]
    assert argv == [sys.executable, "-m", "eggpool", "serve"]
    kwargs = mock_popen.call_args[1]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == subprocess.DEVNULL


def test_ensure_running_clears_stale_pid_before_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "stale.pid"
    pid_file.write_text(str(os.getpid() + 10**8), encoding="utf-8")
    monkeypatch.setattr("eggpool.fastcli.default_pid_file", lambda: pid_file)

    mock_proc = MagicMock()
    with patch(
        "eggpool.fastcli.subprocess.Popen", return_value=mock_proc
    ) as mock_popen:
        result = fastcli._run_ensure_running(None)

    assert result == 0
    assert not pid_file.exists(), "stale PID file must be cleared before spawn"
    mock_popen.assert_called_once()


def test_ensure_running_returns_nonzero_when_spawn_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "missing.pid"
    monkeypatch.setattr("eggpool.fastcli.default_pid_file", lambda: pid_file)
    monkeypatch.setattr("eggpool.fastcli.default_log_file", lambda: tmp_path / "x.log")

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("no such executable")

    with patch("eggpool.fastcli.subprocess.Popen", side_effect=_raise):
        result = fastcli._run_ensure_running(None)

    assert result != 0


def test_ensure_running_uses_absolute_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "missing.pid"
    monkeypatch.setattr("eggpool.fastcli.default_pid_file", lambda: pid_file)

    relative_config = tmp_path / "subdir" / "eggpool.toml"
    relative_config.parent.mkdir(parents=True, exist_ok=True)
    relative_config.write_text("[server]\n", encoding="utf-8")

    mock_proc = MagicMock()
    with patch(
        "eggpool.fastcli.subprocess.Popen", return_value=mock_proc
    ) as mock_popen:
        result = fastcli._run_ensure_running(str(relative_config))

    assert result == 0
    argv = mock_popen.call_args[0][0]
    config_arg_index = argv.index("--config") + 1
    assert Path(argv[config_arg_index]).is_absolute()
    assert Path(argv[config_arg_index]) == relative_config.resolve()
