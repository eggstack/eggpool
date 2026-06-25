"""Lightweight PID/log path resolution helpers.

Stdlib-only on purpose: these helpers are called from the fast-path CLI
dispatcher (``eggpool.fastcli``) which must stay import-cheap for cron
checks on Raspberry Pi-class hardware. Do not import anything from
``eggpool`` or any third-party package from this module.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def state_dir() -> Path:
    """Return ``~/.local/state/eggpool``, creating parent directories if missing.

    Returns the path even when parent creation fails so the caller can
    decide whether to fall back to a UID-scoped ``/tmp`` path. The
    exception is logged at warning level.
    """
    path = Path.home() / ".local" / "state" / "eggpool"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Could not create state dir %s: %s", path, exc)
    return path


def default_pid_file() -> Path:
    """Resolve the live PID file path with this precedence.

    1. ``$EGGPOOL_PID_FILE`` (if set)
    2. ``$XDG_RUNTIME_DIR/eggpool.pid`` (if ``XDG_RUNTIME_DIR`` is set)
    3. ``~/.local/state/eggpool/eggpool.pid`` (parent auto-created)
    4. ``/tmp/eggpool-<UID>.pid`` fallback (UID-scoped, ``/tmp`` not created)
    """
    explicit = os.environ.get("EGGPOOL_PID_FILE")
    if explicit:
        return Path(explicit)

    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        return Path(xdg_runtime) / "eggpool.pid"

    state = state_dir()
    if state.exists():
        return state / "eggpool.pid"

    return Path("/tmp") / f"eggpool-{os.getuid()}.pid"


def default_log_file() -> Path:
    """Return ``$EGGPOOL_LOG_FILE`` if set, else ``<state_dir>/eggpool.log``.

    Ensures the parent directory exists when it can. ``/tmp`` fallback
    is not used here; missing state dir is logged and the path returned
    anyway so the daemon can decide what to do.
    """
    explicit = os.environ.get("EGGPOOL_LOG_FILE")
    if explicit:
        path = Path(explicit)
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
        return path

    state = state_dir()
    path = state / "eggpool.log"
    with contextlib.suppress(OSError):
        state.mkdir(parents=True, exist_ok=True)
    return path


def read_pid_file(path: Path | None = None) -> int | None:
    """Read the PID from ``path`` (or :func:`default_pid_file`).

    Returns ``None`` when the file is missing or unparseable.
    """
    target = path if path is not None else default_pid_file()
    try:
        text = target.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    try:
        return int(text)
    except ValueError:
        return None


def is_process_running(pid: int) -> bool:
    """Return ``True`` if ``os.kill(pid, 0)`` succeeds.

    Signal 0 is the conventional probe: it performs the permission and
    existence checks without delivering any signal. ``ProcessLookupError``
    (process gone) and ``OSError`` (other failure) are treated as not
    running. ``PermissionError`` is also treated as not running: we
    cannot signal the process and would rather the cron job try to
    start a fresh instance than silently succeed on a probe we cannot
    verify.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return False
    return True


def clear_pid_file(path: Path | None = None) -> None:
    """Remove the PID file if present. Never raises."""
    target = path if path is not None else default_pid_file()
    with contextlib.suppress(OSError):
        target.unlink(missing_ok=True)
