"""Unit tests for daemon-mode behavior in :mod:`eggpool.runtime` and
:mod:`eggpool.cli_full`.

Covers three areas:

- :func:`eggpool.runtime.start_server` / :func:`eggpool.runtime.restart_server`
  helpers introduced for the ``--daemon`` spawn path.
- :func:`eggpool.cli_full._serve_daemon` Click-command behavior (root
  refusal, ``--log-file`` plumbing, second-instance detection).
- :data:`eggpool.constants.PID_FILE` lazy proxy.

All subprocess spawns are mocked; the tests do not start a real server.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from eggpool import cli as cli_module
from eggpool import cli_full
from eggpool import runtime as runtime_module
from eggpool.constants import PID_FILE

# ---------------------------------------------------------------------------
# runtime.start_server / runtime.restart_server
# ---------------------------------------------------------------------------


class TestStartServerDaemonSpawn:
    """Behavior of :func:`runtime.start_server` in daemon mode (the default)."""

    def test_start_server_daemon_uses_absolute_config_path(
        self, tmp_path: Path
    ) -> None:
        """The child argv contains an absolute path to the config.

        A detached child has no working-directory guarantee, so the
        config path must be resolved before the spawn.
        """
        relative_config = tmp_path / "subdir" / "eggpool.toml"
        relative_config.parent.mkdir(parents=True, exist_ok=True)
        relative_config.write_text("[server]\n", encoding="utf-8")

        mock_proc = MagicMock()
        with patch.object(
            runtime_module.subprocess, "Popen", return_value=mock_proc
        ) as mock_popen:
            runtime_module.start_server(str(relative_config))

        argv = mock_popen.call_args[0][0]
        config_index = argv.index("--config") + 1
        assert Path(argv[config_index]).is_absolute()
        assert Path(argv[config_index]) == relative_config.resolve()

    def test_start_server_daemon_does_not_pass_daemon_flag(
        self, tmp_path: Path
    ) -> None:
        """The child argv does NOT contain ``--daemon``.

        Detachment is a parent-side concern; the child runs the normal
        foreground ``serve`` command.
        """
        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\n", encoding="utf-8")

        mock_proc = MagicMock()
        with patch.object(
            runtime_module.subprocess, "Popen", return_value=mock_proc
        ) as mock_popen:
            runtime_module.start_server(str(config_path), daemon=True)

        argv = mock_popen.call_args[0][0]
        assert "--daemon" not in argv

    def test_start_server_daemon_passes_serve_command(self, tmp_path: Path) -> None:
        """``serve`` is the last positional argument in the child argv."""
        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\n", encoding="utf-8")

        mock_proc = MagicMock()
        with patch.object(
            runtime_module.subprocess, "Popen", return_value=mock_proc
        ) as mock_popen:
            runtime_module.start_server(str(config_path), daemon=True)

        argv = mock_popen.call_args[0][0]
        assert argv[-1] == "serve"

    def test_start_server_daemon_opens_stdin_devnull(self, tmp_path: Path) -> None:
        """The child's stdin is ``subprocess.DEVNULL``."""
        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\n", encoding="utf-8")

        mock_proc = MagicMock()
        with patch.object(
            runtime_module.subprocess, "Popen", return_value=mock_proc
        ) as mock_popen:
            runtime_module.start_server(str(config_path), daemon=True)

        kwargs = mock_popen.call_args[1]
        assert kwargs["stdin"] == subprocess.DEVNULL

    def test_start_server_daemon_writes_to_log_file(self, tmp_path: Path) -> None:
        """When a log_path is supplied, a file handle for that path is
        passed to ``Popen`` as stdout/stderr.
        """
        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\n", encoding="utf-8")
        log_path = tmp_path / "daemon.log"

        mock_proc = MagicMock()
        with patch.object(
            runtime_module.subprocess, "Popen", return_value=mock_proc
        ) as mock_popen:
            runtime_module.start_server(
                str(config_path), daemon=True, log_path=str(log_path)
            )

        kwargs = mock_popen.call_args[1]
        stdout_target = kwargs["stdout"]
        stderr_target = kwargs["stderr"]
        # The Popen call captures the same open handle for both streams
        assert stdout_target is stderr_target
        # The handle points at the requested log path
        assert hasattr(stdout_target, "name")
        assert Path(stdout_target.name) == log_path

    def test_start_server_daemon_quiet_sends_to_devnull(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ``quiet=True`` and no log path is supplied,
        ``_resolve_daemon_log_path`` returns ``None`` and the child's
        stdout/stderr go to ``subprocess.DEVNULL``.
        """
        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\n", encoding="utf-8")

        monkeypatch.setattr(
            runtime_module,
            "_resolve_daemon_log_path",
            lambda _log, **_kwargs: None,
        )

        mock_proc = MagicMock()
        with patch.object(
            runtime_module.subprocess, "Popen", return_value=mock_proc
        ) as mock_popen:
            runtime_module.start_server(
                str(config_path), daemon=True, log_path=None, quiet=True
            )

        kwargs = mock_popen.call_args[1]
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL

    def test_start_server_daemon_uses_default_log_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ``log_path`` is ``None``, ``default_log_file()`` is used."""
        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\n", encoding="utf-8")
        expected_log = tmp_path / "default.log"

        def _fake_default_log_file() -> Path:
            return expected_log

        monkeypatch.setattr(
            "eggpool.runtime_paths.default_log_file", _fake_default_log_file
        )

        mock_proc = MagicMock()
        with patch.object(
            runtime_module.subprocess, "Popen", return_value=mock_proc
        ) as mock_popen:
            runtime_module.start_server(
                str(config_path), daemon=True, log_path=None, quiet=False
            )

        kwargs = mock_popen.call_args[1]
        stdout_target = kwargs["stdout"]
        assert hasattr(stdout_target, "name")
        assert Path(stdout_target.name) == expected_log

    def test_start_server_daemon_creates_log_parent_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A log path under a missing parent directory is created."""
        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\n", encoding="utf-8")
        nested_log = tmp_path / "nested" / "deeper" / "daemon.log"

        mock_proc = MagicMock()
        with patch.object(
            runtime_module.subprocess, "Popen", return_value=mock_proc
        ) as _mock_popen:
            runtime_module.start_server(
                str(config_path), daemon=True, log_path=str(nested_log)
            )

        assert nested_log.parent.is_dir()

    def test_start_server_daemon_sets_start_new_session(self, tmp_path: Path) -> None:
        """Daemon spawns run in a fresh process group (``start_new_session=True``)."""
        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\n", encoding="utf-8")

        mock_proc = MagicMock()
        with patch.object(
            runtime_module.subprocess, "Popen", return_value=mock_proc
        ) as mock_popen:
            runtime_module.start_server(str(config_path), daemon=True)

        kwargs = mock_popen.call_args[1]
        assert kwargs["start_new_session"] is True

    def test_start_server_foreground_skips_log_redirection(
        self, tmp_path: Path
    ) -> None:
        """When ``daemon=False``, stdin/stdout/stderr are NOT redirected.

        The foreground path is a transparent Popen; the caller controls
        redirection. The default values for those kwargs are ``None``
        (inherit from the parent process).
        """
        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\n", encoding="utf-8")

        mock_proc = MagicMock()
        with patch.object(
            runtime_module.subprocess, "Popen", return_value=mock_proc
        ) as mock_popen:
            runtime_module.start_server(str(config_path), daemon=False)

        kwargs = mock_popen.call_args[1]
        assert "stdin" not in kwargs or kwargs["stdin"] is None
        assert "stdout" not in kwargs or kwargs["stdout"] is None
        assert "stderr" not in kwargs or kwargs["stderr"] is None
        # start_new_session is still set so a Ctrl-C in the calling shell
        # does not propagate to the supervisor.
        assert kwargs["start_new_session"] is True

    def test_start_server_daemon_closes_parent_log_handle(self, tmp_path: Path) -> None:
        """The parent's Python-level file handle is closed after spawn.

        Once :class:`subprocess.Popen` has duplicated the underlying
        file descriptor into the child, the parent no longer needs its
        own handle. The runtime helper closes it in a ``finally`` block
        to avoid leaking descriptors in a long-lived supervisor.
        """
        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\n", encoding="utf-8")
        log_path = tmp_path / "daemon.log"

        captured_handles: list[object] = []

        def _capture_popen(*args: object, **kwargs: object) -> MagicMock:  # noqa: ARG001
            stdout = kwargs.get("stdout")
            if hasattr(stdout, "name"):
                captured_handles.append(stdout)
            return MagicMock()

        with patch.object(
            runtime_module.subprocess, "Popen", side_effect=_capture_popen
        ):
            runtime_module.start_server(
                str(config_path), daemon=True, log_path=str(log_path)
            )

        assert captured_handles, "Popen should have received a file handle"
        for handle in captured_handles:
            assert getattr(handle, "closed", False) is True

    def test_start_server_daemon_verify_waits_for_pid_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify mode calls ``_wait_for_pid_file`` before returning."""
        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\n", encoding="utf-8")

        wait_calls: list[float] = []

        def _fake_wait(timeout_s: float) -> bool:
            wait_calls.append(timeout_s)
            return True

        monkeypatch.setattr(runtime_module, "_wait_for_pid_file", _fake_wait)

        mock_proc = MagicMock()
        with patch.object(runtime_module.subprocess, "Popen", return_value=mock_proc):
            runtime_module.start_server(
                str(config_path),
                daemon=True,
                verify=True,
                verify_timeout_s=2.5,
            )

        assert wait_calls == [2.5]

    def test_start_server_daemon_verify_timeout_returns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_wait_for_pid_file`` returns ``False`` when the PID file never appears."""
        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\n", encoding="utf-8")

        # No PID file exists in tmp_path; the helper must return False.
        assert runtime_module._wait_for_pid_file(0.05) is False

    def test_start_server_raises_on_spawn_failure(self, tmp_path: Path) -> None:
        """``OSError`` from ``Popen`` is not caught."""
        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\n", encoding="utf-8")

        def _raise(*_args: object, **_kwargs: object) -> object:
            raise FileNotFoundError(2, "No such file or directory", sys.executable)

        with (
            patch.object(runtime_module.subprocess, "Popen", side_effect=_raise),
            pytest.raises(FileNotFoundError),
        ):
            runtime_module.start_server(str(config_path))


class TestRestartServerDaemonDefault:
    """Behavior of :func:`runtime.restart_server` when ``daemon`` defaults."""

    def test_restart_server_uses_daemon_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``restart_server`` spawns a detached child by default.

        The default ``daemon=True`` forwards to :func:`start_server`,
        which sets ``start_new_session=True`` and ``stdin=DEVNULL``.
        """
        config_path = tmp_path / "config.toml"
        config_path.write_text("[server]\n", encoding="utf-8")
        pid_file = tmp_path / "eggpool.pid"
        pid_file.write_text("42", encoding="utf-8")
        monkeypatch.setattr("eggpool.constants.PID_FILE", pid_file)

        # Pretend the supervisor is alive so restart proceeds to spawn.
        monkeypatch.setattr(runtime_module, "is_process_running", lambda _pid: True)
        monkeypatch.setattr(runtime_module, "send_sigterm", lambda _pid: True)
        # wait_for_exit polls is_process_running in a loop; if it never
        # returns False the loop runs to the timeout. Stub it directly.
        monkeypatch.setattr(runtime_module, "wait_for_exit", lambda _pid, _t: True)
        monkeypatch.setattr(runtime_module, "clear_pid_file", lambda: None)

        mock_proc = MagicMock()
        with patch.object(
            runtime_module.subprocess, "Popen", return_value=mock_proc
        ) as mock_popen:
            result = runtime_module.restart_server(str(config_path))

        assert result is True
        kwargs = mock_popen.call_args[1]
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] == subprocess.DEVNULL


# ---------------------------------------------------------------------------
# _serve_daemon CLI behavior
# ---------------------------------------------------------------------------


def _write_minimal_config(tmp_path: Path, *, with_account: bool = False) -> Path:
    """Write a minimal valid TOML config for ``AppConfig.from_toml``."""
    config_path = tmp_path / "config.toml"
    body = '[server]\nhost = "127.0.0.1"\nport = 11300\n'
    if with_account:
        body += (
            "\n[providers.test]\n"
            'id = "test"\n'
            'base_url = "https://example.com/v1"\n'
            'protocols = ["openai"]\n'
            "\n[providers.test.auth]\n"
            'mode = "bearer"\n'
            'header = "Authorization"\n'
            'scheme = "Bearer"\n'
            "\n[[providers.test.accounts]]\n"
            'name = "default"\n'
            'api_key = "sk-test-key-1234567890"\n'
            "enabled = true\n"
            "weight = 1.0\n"
        )
    config_path.write_text(body, encoding="utf-8")
    return config_path


class TestServeDaemonCli:
    """Behavior of :func:`cli_full._serve_daemon` invoked via the ``serve`` command."""

    def test_serve_daemon_refuses_as_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``eggpool serve --daemon`` exits 1 with a root-refusal message."""
        config_path = _write_minimal_config(tmp_path)
        monkeypatch.setattr(os, "geteuid", lambda: 0)

        runner = CliRunner()
        result = runner.invoke(
            cli_module.cli,
            ["--config", str(config_path), "serve", "--daemon"],
        )

        assert result.exit_code == 1
        assert "root" in (result.stderr or result.output).lower()

    def test_serve_daemon_allows_as_root_with_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With ``--as-root``, ``eggpool serve --daemon`` does not refuse.

        The parent proceeds to :func:`_serve_daemon`, which in turn
        calls :func:`runtime.start_server` (mocked here).
        """
        config_path = _write_minimal_config(tmp_path)
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        pid_file = tmp_path / "eggpool.pid"
        monkeypatch.setattr("eggpool.constants.PID_FILE", pid_file)

        # No second instance.
        monkeypatch.setattr(runtime_module, "read_pid", lambda: None)
        monkeypatch.setattr(runtime_module, "is_process_running", lambda _pid: False)
        monkeypatch.setattr(runtime_module, "probe_healthz", lambda *_a, **_k: False)
        monkeypatch.setattr(runtime_module, "clear_pid_file", lambda: None)

        log_path = tmp_path / "daemon.log"
        monkeypatch.setattr("eggpool.runtime_paths.default_log_file", lambda: log_path)
        monkeypatch.setattr("eggpool.runtime_paths.default_pid_file", lambda: pid_file)

        mock_proc = MagicMock()
        mock_proc.pid = 99999
        with patch.object(
            runtime_module, "start_server", return_value=mock_proc
        ) as mock_start:
            runner = CliRunner()
            result = runner.invoke(
                cli_module.cli,
                [
                    "--config",
                    str(config_path),
                    "serve",
                    "--daemon",
                    "--as-root",
                ],
            )

        assert result.exit_code == 0, result.output
        assert mock_start.called
        # The first positional argument is the config path.
        assert mock_start.call_args[0][0] == str(config_path)
        # daemon=True is the default; verify it was forwarded.
        assert mock_start.call_args[1].get("daemon") is True

    def test_serve_daemon_passes_log_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--log-file PATH`` is forwarded as ``log_path`` to ``start_server``."""
        config_path = _write_minimal_config(tmp_path)
        pid_file = tmp_path / "eggpool.pid"
        monkeypatch.setattr("eggpool.constants.PID_FILE", pid_file)

        monkeypatch.setattr(runtime_module, "read_pid", lambda: None)
        monkeypatch.setattr(runtime_module, "is_process_running", lambda _pid: False)
        monkeypatch.setattr(runtime_module, "probe_healthz", lambda *_a, **_k: False)
        monkeypatch.setattr(runtime_module, "clear_pid_file", lambda: None)
        monkeypatch.setattr("eggpool.runtime_paths.default_pid_file", lambda: pid_file)

        explicit_log = tmp_path / "explicit.log"
        monkeypatch.setattr(
            "eggpool.runtime_paths.default_log_file", lambda: explicit_log
        )

        mock_proc = MagicMock()
        mock_proc.pid = 4242
        with patch.object(
            runtime_module, "start_server", return_value=mock_proc
        ) as mock_start:
            runner = CliRunner()
            result = runner.invoke(
                cli_module.cli,
                [
                    "--config",
                    str(config_path),
                    "serve",
                    "--daemon",
                    "--log-file",
                    str(explicit_log),
                ],
            )

        assert result.exit_code == 0, result.output
        assert mock_start.called
        assert mock_start.call_args[1].get("log_path") == str(explicit_log)

    def test_serve_daemon_refuses_when_server_already_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exits 1 when the PID file points at a live process."""
        config_path = _write_minimal_config(tmp_path)
        pid_file = tmp_path / "eggpool.pid"
        monkeypatch.setattr("eggpool.constants.PID_FILE", pid_file)
        pid_file.write_text("99999", encoding="utf-8")

        monkeypatch.setattr(runtime_module, "read_pid", lambda: 99999)
        monkeypatch.setattr(runtime_module, "is_process_running", lambda _pid: True)
        monkeypatch.setattr(runtime_module, "probe_healthz", lambda *_a, **_k: False)

        start_calls: list[object] = []
        with patch.object(
            runtime_module, "start_server", side_effect=start_calls.append
        ):
            runner = CliRunner()
            result = runner.invoke(
                cli_module.cli,
                ["--config", str(config_path), "serve", "--daemon"],
            )

        assert result.exit_code == 1
        assert "already running" in (result.stderr or result.output).lower()
        assert start_calls == []

    def test_serve_daemon_refuses_when_healthz_responds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exits 1 when ``probe_healthz`` returns ``True`` (foreign server)."""
        config_path = _write_minimal_config(tmp_path)
        pid_file = tmp_path / "eggpool.pid"
        monkeypatch.setattr("eggpool.constants.PID_FILE", pid_file)

        monkeypatch.setattr(runtime_module, "read_pid", lambda: None)
        monkeypatch.setattr(runtime_module, "is_process_running", lambda _pid: False)
        monkeypatch.setattr(runtime_module, "probe_healthz", lambda *_a, **_k: True)

        start_calls: list[object] = []
        with patch.object(
            runtime_module, "start_server", side_effect=start_calls.append
        ):
            runner = CliRunner()
            result = runner.invoke(
                cli_module.cli,
                ["--config", str(config_path), "serve", "--daemon"],
            )

        assert result.exit_code == 1
        assert "another process" in (result.stderr or result.output).lower()
        assert start_calls == []

    def test_serve_daemon_spawns_and_reports_pid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Successful spawn reports the supervisor PID and the log path."""
        config_path = _write_minimal_config(tmp_path)
        pid_file = tmp_path / "eggpool.pid"
        monkeypatch.setattr("eggpool.constants.PID_FILE", pid_file)

        monkeypatch.setattr(runtime_module, "read_pid", lambda: None)
        monkeypatch.setattr(runtime_module, "is_process_running", lambda _pid: False)
        monkeypatch.setattr(runtime_module, "probe_healthz", lambda *_a, **_k: False)
        monkeypatch.setattr(runtime_module, "clear_pid_file", lambda: None)
        monkeypatch.setattr("eggpool.runtime_paths.default_pid_file", lambda: pid_file)

        log_path = tmp_path / "daemon.log"
        monkeypatch.setattr("eggpool.runtime_paths.default_log_file", lambda: log_path)

        mock_proc = MagicMock()
        mock_proc.pid = 54321
        with patch.object(runtime_module, "start_server", return_value=mock_proc):
            runner = CliRunner()
            result = runner.invoke(
                cli_module.cli,
                ["--config", str(config_path), "serve", "--daemon"],
            )

        assert result.exit_code == 0, result.output
        assert "54321" in result.output
        assert str(pid_file) in result.output
        assert str(log_path) in result.output
        assert "croncheck" in result.output.lower()

    def test_serve_daemon_unit_helper_returns_on_spawn_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A direct ``_serve_daemon`` surfaces ``OSError`` via ``SystemExit``."""
        config_path = _write_minimal_config(tmp_path)
        monkeypatch.setattr("eggpool.constants.PID_FILE", tmp_path / "eggpool.pid")
        monkeypatch.setattr(runtime_module, "read_pid", lambda: None)
        monkeypatch.setattr(runtime_module, "is_process_running", lambda _pid: False)
        monkeypatch.setattr(runtime_module, "probe_healthz", lambda *_a, **_k: False)
        monkeypatch.setattr(runtime_module, "clear_pid_file", lambda: None)

        def _raise(*_args: object, **_kwargs: object) -> object:
            raise OSError(2, "No such file or directory")

        with patch.object(runtime_module, "start_server", side_effect=_raise):
            ctx = cli_module.cli.make_context(
                "cli",
                ["--config", str(config_path), "serve", "--daemon"],
                resilient_parsing=True,
            )
            with pytest.raises(SystemExit) as exc_info:
                cli_full._serve_daemon(
                    ctx, str(config_path), log_file=None, quiet=False
                )
            assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# PID_FILE proxy
# ---------------------------------------------------------------------------


class TestPIDFileProxy:
    """Behavior of :class:`eggpool.constants._PIDFileProxy`."""

    def test_pid_file_proxy_forwards_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``PID_FILE.exists()`` resolves through ``default_pid_file``."""
        pid_file = tmp_path / "live.pid"
        pid_file.write_text("12345", encoding="utf-8")
        monkeypatch.setenv("EGGPOOL_PID_FILE", str(pid_file))
        assert PID_FILE.exists() is True

        # Re-point to a missing file via env; the proxy must re-resolve.
        monkeypatch.setenv("EGGPOOL_PID_FILE", str(tmp_path / "missing.pid"))
        assert PID_FILE.exists() is False

    def test_pid_file_proxy_forwards_read_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``PID_FILE.read_text()`` returns the resolved file's contents."""
        pid_file = tmp_path / "live.pid"
        pid_file.write_text("98765", encoding="utf-8")
        monkeypatch.setenv("EGGPOOL_PID_FILE", str(pid_file))
        assert PID_FILE.read_text(encoding="utf-8").strip() == "98765"

    def test_pid_file_proxy_supports_truediv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``PID_FILE / "x"`` returns a :class:`pathlib.Path`."""
        monkeypatch.setenv("EGGPOOL_PID_FILE", str(tmp_path / "eggpool.pid"))
        result = PID_FILE / "eggpool.sock"
        assert isinstance(result, Path)
        assert result == tmp_path / "eggpool.pid" / "eggpool.sock"

    def test_pid_file_proxy_supports_fspath(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``os.fspath(PID_FILE)`` returns the resolved path as a string."""
        monkeypatch.setenv("EGGPOOL_PID_FILE", str(tmp_path / "eggpool.pid"))
        fspath = os.fspath(PID_FILE)
        assert isinstance(fspath, str)
        assert fspath == str(tmp_path / "eggpool.pid")

    def test_pid_file_proxy_resolves_each_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The proxy re-resolves on every attribute access, picking up env changes."""
        first = tmp_path / "first.pid"
        second = tmp_path / "second.pid"

        monkeypatch.setenv("EGGPOOL_PID_FILE", str(first))
        assert PID_FILE.read_text.__self__ == first  # type: ignore[attr-defined]
        # Read directly to confirm the value, not just the bound method's self.
        assert str(PID_FILE) == str(first)

        monkeypatch.setenv("EGGPOOL_PID_FILE", str(second))
        assert str(PID_FILE) == str(second)
