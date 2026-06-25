"""Server process runtime helpers.

This module centralizes process-lifecycle primitives that were previously
duplicated across ``eggpool.cli`` and ``eggpool.providers.connect``:

- reading the PID file
- checking whether a PID is alive
- waiting for a process to exit
- starting the server in the background
- restarting the running server

Putting these helpers in one place ensures consistent behavior (timeout
handling, error reporting, cleanup of stale PID files) and avoids the
drift that had accumulated in the old inline implementations.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)


DEFAULT_SHUTDOWN_TIMEOUT_S = 10.0


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
    parent CLI do not propagate to the server.
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
    could not be completed within the timeout.
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
