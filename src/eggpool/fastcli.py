"""Stdlib-only fast-path CLI dispatcher.

Why this exists
---------------
Recurring status and watchdog commands (``croncheck``, ``ensure-running``)
should be cheap to start on Raspberry Pi-class hardware where every cron
invocation matters. The full Click CLI in :mod:`eggpool.cli_full` imports
substantial portions of the application stack at module load time; that
is overkill for a five-line PID probe.

This module deliberately uses only the Python standard library so the
fast path does not pull in ``fastapi``, ``granian``, ``httpx``,
``pydantic``, ``aiosqlite``, or any ``eggpool`` subpackage. The fast
path is responsible only for the two lightweight commands; everything
else falls through to the normal Click CLI.

Public API
----------
:func:`maybe_run_fast_command` runs the fast path when ``argv`` matches
a recognized command and returns an integer exit code. It returns
``None`` when the command is not a fast-path command, in which case the
caller should dispatch to the normal full CLI.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from eggpool.runtime_paths import (
    clear_pid_file,
    default_log_file,
    default_pid_file,
    is_process_running,
    read_pid_file,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_FAST_COMMANDS: frozenset[str] = frozenset({"croncheck", "ensure-running"})


def _parse_simple_argv(
    argv: Sequence[str],
) -> tuple[str | None, str | None]:
    """Pull ``--config PATH`` (or ``--config=PATH`` or ``-c PATH``) and a
    recognized fast-path command out of ``argv``.

    The parser is intentionally tiny: it walks every argument once,
    tracks the most recently seen config path, and remembers the first
    non-flag argument that looks like a recognized command. Unknown
    flags are skipped and, when they take a value, the following
    non-flag argument is consumed. A flag-like argument is never
    treated as a command.
    """
    config_path: str | None = None
    command: str | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--config" or arg == "-c":
            if i + 1 < len(argv):
                config_path = argv[i + 1]
                i += 2
                continue
            i += 1
            continue
        if arg.startswith("--config="):
            config_path = arg.split("=", 1)[1]
            i += 1
            continue
        if arg.startswith("--") or (arg.startswith("-") and len(arg) > 1):
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                i += 2
                continue
            i += 1
            continue
        if command is None and arg in _FAST_COMMANDS:
            command = arg
        i += 1
    return config_path, command


def _run_croncheck(config_path: str | None) -> int:
    """Fast-path ``croncheck``.

    Returns ``0`` when the PID file points at a live process, ``1`` when
    the file is missing/invalid or points at a dead process. ``2`` is
    reserved for internal errors but currently unused.
    """
    pid_file = default_pid_file()
    if not pid_file.exists():
        return 1
    pid = read_pid_file(pid_file)
    if pid is None:
        return 1
    if is_process_running(pid):
        return 0
    return 1


def _spawn_daemon(config_path: str | None) -> int:
    """Spawn the server in the background.

    The child runs the normal foreground ``serve`` command so it can
    write its own PID file through the existing supervisor path; it
    does not re-enter this fast-path module.
    """
    argv: list[str] = [sys.executable, "-m", "eggpool"]
    if config_path:
        argv.extend(["--config", str(Path(config_path).resolve())])
    argv.append("serve")

    log_path = default_log_file()
    try:
        log_handle = open(log_path, "ab")  # noqa: SIM115 - intentional append
    except OSError as exc:
        print(
            f"ensure-running: cannot open log file {log_path}: {exc}", file=sys.stderr
        )
        return 1

    try:
        subprocess.Popen(  # noqa: S603 - intentional spawn
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
        )
    except (OSError, FileNotFoundError) as exc:
        print(f"ensure-running: failed to spawn server: {exc}", file=sys.stderr)
        return 1
    finally:
        log_handle.close()
    return 0


def _run_ensure_running(config_path: str | None) -> int:
    """Fast-path ``ensure-running``.

    Exits ``0`` quickly when the server is already alive. Clears stale
    PID state before spawning, and returns ``0`` once the child has been
    spawned. Returns non-zero on spawn failure.
    """
    pid_file = default_pid_file()
    if pid_file.exists():
        pid = read_pid_file(pid_file)
        if pid is not None and is_process_running(pid):
            return 0
        clear_pid_file(pid_file)
    return _spawn_daemon(config_path)


def maybe_run_fast_command(argv: Sequence[str]) -> int | None:
    """Run a fast-path command if ``argv`` matches one.

    Returns an integer exit code when the command was handled.
    Returns ``None`` when the normal full CLI should handle the command.
    """
    config_path, command = _parse_simple_argv(argv)
    if command == "croncheck":
        return _run_croncheck(config_path)
    if command == "ensure-running":
        return _run_ensure_running(config_path)
    return None
