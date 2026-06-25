"""Server process runtime helpers.

This module centralizes process-lifecycle primitives that were previously
duplicated across ``eggpool.cli`` and ``eggpool.providers.connect``:

- reading and clearing the PID file
- checking whether a PID is alive
- waiting for a process to exit
- probing the data plane for an already-running server
- starting the server in the background
- restarting the running server

The PID file is owned by the **supervisor** process: the CLI's
``serve`` command writes ``os.getpid()`` before calling
``Granian(...).serve()`` and removes the file in a ``finally`` block.
Granian's supervisor + single-worker model means the worker is a
child of the supervisor; signaling the supervisor PID stops both
processes cleanly. See ``plans/fix_granian_single_process.md`` for
the full design.

Putting these helpers in one place ensures consistent behavior
(timeout handling, error reporting, cleanup of stale PID files) and
avoids the drift that had accumulated in the old inline
implementations.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)


DEFAULT_SHUTDOWN_TIMEOUT_S = 10.0
DEFAULT_HEALTH_PROBE_TIMEOUT_S: Final = 1.0


def _pid_file() -> Path:
    """Resolve the live PID file path from ``eggpool.constants``.

    Imported lazily on each call so tests that monkey-patch
    ``eggpool.constants.PID_FILE`` see the patched value instead of the
    one captured at import time.
    """
    from eggpool.constants import PID_FILE

    return PID_FILE


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


def start_server(
    config_path: str, *, cwd: str | None = None
) -> subprocess.Popen[bytes]:
    """Spawn a fresh server in the background.

    The returned ``Popen`` handle is intentionally not awaited; the new
    process detaches via ``start_new_session=True`` so signals to the
    parent CLI do not propagate to the server. The new process is the
    supervisor: it writes its own PID via ``write_pid_file()`` before
    calling ``Granian(...).serve()`` and clears the file in a
    ``finally`` block. The caller can wait on the returned handle if
    it needs to surface the exit code; the typical CLI path does not.
    """
    resolved = str(Path(config_path).resolve())
    argv = [sys.executable, "-m", "eggpool", "--config", resolved, "serve"]
    return subprocess.Popen(  # noqa: S602,S603 - intentional spawn
        argv,
        cwd=cwd or os.getcwd(),
        start_new_session=True,
    )


def restart_server(
    config_path: str, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_S
) -> bool:
    """Stop the running server (if any) and start a new one.

    Returns ``True`` when a fresh server was successfully spawned,
    ``False`` when no server was previously running or the restart
    could not be completed within the timeout. The supervisor PID is
    read from the PID file written by the running ``serve`` command,
    not from the worker PID (the lifespan no longer touches the PID
    file). Stopping the supervisor also tears down its worker, so
    the restart is clean.
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
    start_server(config_path)
    return True
