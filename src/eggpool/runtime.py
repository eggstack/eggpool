"""Server process runtime helpers.

This module centralizes process-lifecycle primitives that were previously
duplicated across ``eggpool.cli`` and ``eggpool.providers.connect``:

- reading and clearing the PID file
- checking whether a PID is alive
- waiting for a process to exit
- probing the data plane for an already-running server
- starting the server in the background (foreground or daemon mode)
- restarting the running server

The PID file is owned by the **supervisor** process: the CLI's
``serve`` command writes ``os.getpid()`` before calling
``Granian(...).serve()`` and removes the file in a ``finally`` block.
Granian's supervisor + single-worker model means the worker is a
child of the supervisor; signaling the supervisor PID stops both
processes cleanly.

Putting these helpers in one place ensures consistent behavior
(timeout handling, error reporting, cleanup of stale PID files,
daemon-mode log redirection) and avoids the drift that had
accumulated in the old inline implementations.

Daemon mode
-----------
``start_server(..., daemon=True)`` spawns a detached supervisor that
runs the normal foreground ``serve`` command. The child's stdin is
closed (``/dev/null``) and stdout/stderr are appended to a log file
under the operator's state directory, so the spawning shell is not
tied to the new process and Granian logs survive a closed terminal.
The child command is **not** passed any ``--daemon`` flag; the
detachment is purely a parent-side concern.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import IO, Final

logger = logging.getLogger(__name__)


DEFAULT_SHUTDOWN_TIMEOUT_S = 10.0
DEFAULT_HEALTH_PROBE_TIMEOUT_S: Final = 1.0
DEFAULT_DAEMON_VERIFY_TIMEOUT_S: Final = 3.0


def _pid_file() -> Path:
    """Resolve the live PID file path from :data:`eggpool.constants.PID_FILE`.

    The constant is a lazy proxy that resolves through
    :func:`eggpool.runtime_paths.default_pid_file` on every read, so
    tests that monkey-patch ``eggpool.constants.PID_FILE`` with a
    concrete :class:`pathlib.Path` still work unchanged: the proxy is
    replaced by the test value and the runtime helpers consume the
    test path directly. The proxy default ensures production callers
    always see the current ``$EGGPOOL_PID_FILE`` / ``$XDG_RUNTIME_DIR``
    / state-dir / ``/tmp/<UID>`` precedence without re-implementing
    the resolver at every call site.

    Imported lazily on each call so tests that monkey-patch the
    constant see the patched value instead of the one captured at
    import time.
    """
    from eggpool.constants import PID_FILE

    return Path(os.fspath(PID_FILE))


def read_pid() -> int | None:
    """Read the PID from the PID file, or ``None`` if missing/invalid."""
    path = _pid_file()
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def write_pid_file(pid: int | None = None) -> None:
    """Write ``pid`` (default: current process) to the PID file.

    Best-effort: a failure to write is logged at warning level so
    startup is never blocked by filesystem issues. The caller is the
    supervisor process (``serve`` command), not the ASGI worker.
    """
    path = _pid_file()
    target = pid if pid is not None else os.getpid()
    try:
        path.write_text(str(target), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write PID file %s: %s", path, exc)


def clear_pid_file() -> None:
    """Remove the PID file, ignoring missing-file errors."""
    with contextlib.suppress(OSError):
        _pid_file().unlink(missing_ok=True)


def is_process_running(pid: int) -> bool:
    """Return ``True`` if a process with ``pid`` is currently running.

    Sends signal 0 (which has no effect but raises ``ProcessLookupError``
    if the process is gone) so we do not need platform-specific ps calls.
    """
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def wait_for_exit(pid: int, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_S) -> bool:
    """Wait up to ``timeout`` seconds for ``pid`` to exit.

    Returns ``True`` if the process exited within the timeout, ``False``
    otherwise.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_process_running(pid):
            return True
        time.sleep(0.1)
    return False


def send_sigterm(pid: int) -> bool:
    """Send SIGTERM to ``pid``. Returns False on any signal-send error."""
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError) as exc:
        logger.debug("SIGTERM to %s failed: %s", pid, exc)
        return False


def probe_healthz(
    host: str = "127.0.0.1",
    port: int = 11300,
    *,
    timeout_s: float = DEFAULT_HEALTH_PROBE_TIMEOUT_S,
) -> bool:
    """Return ``True`` if ``GET /v1/healthz`` on ``host:port`` answers 200.

    Used to detect an already-running server before launching a second
    instance, even when the running server was started by a different
    installation (no shared PID file) or by a stale process that
    escaped the supervisor's lifecycle. The probe is intentionally
    short and uses the standard library to avoid a runtime dependency
    on ``httpx`` for this one CLI check.

    Bind-address ``0.0.0.0`` is rewritten to ``127.0.0.1`` so the
    probe targets the local listener.
    """
    target_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{target_host}:{port}/v1/healthz"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310 - URL built from operator config
            return response.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False


def stop_server(timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_S) -> bool:
    """Stop the running server, if any.

    Returns ``True`` only when the server was confirmed stopped within
    ``timeout`` seconds. Stale PID files are cleaned up silently.
    """
    pid = read_pid()
    if pid is None:
        return False
    if not is_process_running(pid):
        clear_pid_file()
        return False
    send_sigterm(pid)
    if wait_for_exit(pid, timeout):
        clear_pid_file()
        return True
    return False


def _resolve_daemon_log_path(log_path: str | None, *, quiet: bool) -> Path | None:
    """Pick a concrete log file path for daemon mode.

    Precedence: explicit ``log_path`` argument, ``$EGGPOOL_LOG_FILE``,
    the resolver's default state-dir log. Returns ``None`` only when
    ``quiet=True`` and the operator did not specify any path, in which
    case the child should have its stdout/stderr sent to ``/dev/null``.
    """
    if log_path:
        path = Path(log_path)
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
        return path

    if quiet:
        return None

    from eggpool.runtime_paths import default_log_file

    return default_log_file()


def _open_daemon_streams(
    log_path: Path | None, *, quiet: bool
) -> tuple[IO[bytes] | int, IO[bytes] | int]:
    """Open stdout/stderr targets for the daemon child.

    Returns ``(stdout, stderr)`` as either a ``Path``-backed append
    handle or ``subprocess.DEVNULL``. When ``quiet=True`` and no
    ``log_path`` was supplied, both streams go to ``/dev/null`` so the
    spawning shell is not tied to the child. The file handles returned
    here are owned by the caller; the child inherits the underlying
    file descriptor and the caller is responsible for closing the
    Python handle after ``Popen`` has duplicated it.
    """
    if log_path is not None:
        handle = open(log_path, "ab")  # noqa: SIM115 - intentional append
        return handle, handle
    if quiet:
        return subprocess.DEVNULL, subprocess.DEVNULL
    # No log file and not quiet: still detach from the parent terminal
    # so the spawning shell is not blocked on a stdout pipe. Operators
    # who want a log file can set $EGGPOOL_LOG_FILE or pass --log-file.
    return subprocess.DEVNULL, subprocess.DEVNULL


def start_server(  # noqa: PLR0913 - daemon options are explicit by design
    config_path: str,
    *,
    cwd: str | None = None,
    daemon: bool = True,
    log_path: str | None = None,
    quiet: bool = True,
    verify: bool = False,
    verify_timeout_s: float = DEFAULT_DAEMON_VERIFY_TIMEOUT_S,
) -> subprocess.Popen[bytes]:
    """Spawn a fresh server in the background.

    The returned ``Popen`` handle is intentionally not awaited; the new
    process detaches via ``start_new_session=True`` so signals to the
    parent CLI do not propagate to the server. The new process is the
    supervisor: it writes its own PID via ``write_pid_file()`` before
    calling ``Granian(...).serve()`` and clears the file in a
    ``finally`` block. The caller can wait on the returned handle if
    it needs to surface the exit code; the typical CLI path does not.

    Daemon behavior
    ---------------
    When ``daemon=True`` (the default), the child is launched with
    stdin closed and stdout/stderr redirected away from the parent
    terminal. The child command itself is **always** the normal
    foreground ``serve`` invocation; the ``--daemon`` flag is never
    forwarded to the child. Log destination precedence:

    1. ``log_path`` argument
    2. ``$EGGPOOL_LOG_FILE`` (via :func:`runtime_paths.default_log_file`)
    3. ``~/.local/state/eggpool/eggpool.log`` (the resolver default)

    When ``quiet=True`` and no log path is supplied, the streams go to
    ``/dev/null``. When ``quiet=False``, an unset log path still
    detaches from the terminal; operators who want Granian logs
    captured to disk must supply ``log_path`` or set the env var.

    When ``verify=True``, the call returns only after the child has
    written its PID file or the verify timeout has elapsed. The
    verify is best-effort: a slow supervisor that fails to write the
    PID file within the timeout is still considered a successful
    spawn, because the supervisor may legitimately take a few seconds
    to parse its config and start Granian.

    The function raises :class:`OSError` (or a subclass) when the
    spawn itself fails. Permission errors opening the log file and
    missing executables both surface as ``OSError``/``FileNotFoundError``
    and are not caught here.
    """
    resolved = str(Path(config_path).resolve())
    argv = [sys.executable, "-m", "eggpool", "--config", resolved, "serve"]

    if not daemon:
        return subprocess.Popen(  # noqa: S602,S603 - intentional spawn
            argv,
            cwd=cwd or os.getcwd(),
            start_new_session=True,
        )

    log_target = _resolve_daemon_log_path(log_path, quiet=quiet)
    stdout_target, stderr_target = _open_daemon_streams(log_target, quiet=quiet)
    try:
        proc = subprocess.Popen(  # noqa: S602,S603 - intentional spawn
            argv,
            cwd=cwd or os.getcwd(),
            stdin=subprocess.DEVNULL,
            stdout=stdout_target,
            stderr=stderr_target,
            start_new_session=True,
        )
    finally:
        # Close the parent's copy of the file descriptor once Popen has
        # duplicated it into the child. The child still has its own
        # open handle, so the log file is not orphaned; the parent no
        # longer holds a Python-level reference to it.
        if log_target is not None:
            for stream in (stdout_target, stderr_target):
                if isinstance(stream, io.IOBase):
                    with contextlib.suppress(OSError):
                        stream.close()

    if verify:
        _wait_for_pid_file(verify_timeout_s)

    return proc


def _wait_for_pid_file(timeout_s: float) -> bool:
    """Wait up to ``timeout_s`` for the supervisor's PID file to appear.

    Returns ``True`` when the file exists and contains a parseable PID
    within the timeout, ``False`` otherwise. A parseable PID does not
    imply the process is alive; the caller's downstream probe handles
    liveness.
    """
    from eggpool.runtime_paths import default_pid_file, read_pid_file

    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() < deadline:
        if read_pid_file(default_pid_file()) is not None:
            return True
        time.sleep(0.05)
    return False


def restart_server(
    config_path: str,
    timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_S,
    *,
    daemon: bool = True,
    log_path: str | None = None,
    quiet: bool = True,
) -> bool:
    """Stop the running server (if any) and start a new one.

    Returns ``True`` when a fresh server was successfully spawned,
    ``False`` when no server was previously running or the restart
    could not be completed within the timeout. The supervisor PID is
    read from the PID file written by the running ``serve`` command,
    not from the worker PID (the lifespan no longer touches the PID
    file). Stopping the supervisor also tears down its worker, so
    the restart is clean.

    The new supervisor is launched with the same daemon/log options
    the operator would have used for ``eggpool serve --daemon``; the
    restart is always detached. Pass ``daemon=False`` only for tests
    that need to drive the supervisor synchronously.
    """
    pid = read_pid()
    if pid is None or not is_process_running(pid):
        return False

    if not send_sigterm(pid):
        return False

    if not wait_for_exit(pid, timeout):
        logger.warning(
            "Server (PID %s) did not stop within %ss; aborting restart.",
            pid,
            timeout,
        )
        return False

    clear_pid_file()
    start_server(
        config_path,
        daemon=daemon,
        log_path=log_path,
        quiet=quiet,
    )
    return True
