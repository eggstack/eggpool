"""Interactive onboarding script for new EggPool installations."""

from __future__ import annotations

import os
import sys
import termios
import tty


def _prompt_yn(message: str) -> bool:
    """Prompt user with a y/n question.

    Returns True if user enters 'y' or 'Y', False otherwise.
    Handles raw terminal input for consistent behavior.
    """
    sys.stdout.write(f"{message} (y/n): ")
    sys.stdout.flush()

    fd = sys.stdin.fileno()
    old_settings = None
    try:
        old_settings = termios.tcgetattr(fd)
        tty.setraw(fd)

        while True:
            raw = os.read(fd, 1)
            if not raw:
                return False
            ch = raw.decode("ascii", errors="replace")

            if ch in ("\r", "\n"):
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                return False
            if ch in ("y", "Y"):
                sys.stdout.write("y\r\n")
                sys.stdout.flush()
                return True
            if ch in ("n", "N", "\x03", "\x1b", "\x04", "q", "Q"):
                sys.stdout.write("n\r\n")
                sys.stdout.flush()
                return False
            # Ignore other characters (e.g. arrow keys, space, tab)
            # and keep reading so we don't spin the CPU.
            sys.stdout.write(ch)
            sys.stdout.flush()
    finally:
        if old_settings is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _prompt_add_another() -> bool:
    """Ask user if they want to add another provider.

    Returns True if user wants to add another, False to continue.
    """
    return _prompt_yn("Add another provider?")


def _ensure_config_with_api_key(config_path: str) -> None:
    """Ensure a server API key exists in the config file.

    The config file itself is created by ``ensure_config()`` which runs
    before any CLI subcommand.  This helper only handles API key generation.
    """
    from pathlib import Path

    path = Path(config_path)

    # Generate a server API key if one doesn't exist
    import tomllib

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    server = raw.get("server", {})
    existing_key = server.get("api_key", "")

    if not existing_key:
        from eggpool.cli import generate_api_key, write_server_api_key

        new_key = generate_api_key()
        write_server_api_key(config_path, new_key)
        sys.stdout.write("  Generated server API key\n")


def run_onboarding(config_path: str, providers_path: str | None = None) -> None:
    """Run the interactive onboarding flow.

    1. Ensure config exists with a server API key
    2. Loop: connect a provider, ask if they want another
    3. Run check-config
    4. Start the server (if not already running)
    """
    sys.stdout.write("\n=== EggPool Onboarding ===\n\n")

    # Ensure we have a config file with a server API key
    sys.stdout.write("--- Setting Up Configuration ---\n")
    from eggpool.config import ensure_config

    ensure_config(config_path)
    _ensure_config_with_api_key(config_path)

    from eggpool.providers.connect import connect as do_connect

    # Interactive provider connection loop
    connected_count = 0
    while True:
        sys.stdout.write(f"--- Connect Provider ({connected_count + 1}) ---\n")
        try:
            ok = do_connect(config_path, providers_path)
        except KeyboardInterrupt:
            sys.stdout.write("\n")
            break

        if ok:
            connected_count += 1

        # Ask if they want to add another
        try:
            if not _prompt_add_another():
                break
        except KeyboardInterrupt:
            sys.stdout.write("\n")
            break

    sys.stdout.write(f"\nConnected {connected_count} provider(s).\n\n")

    # Run check-config
    sys.stdout.write("--- Validating Configuration ---\n")
    import subprocess
    import sys as _sys

    result = subprocess.run(  # noqa: S603
        [_sys.executable, "-m", "eggpool", "--config", config_path, "check-config"],
        cwd=os.getcwd(),
    )
    if result.returncode != 0:
        sys.stdout.write(
            "\nConfiguration check failed. Fix errors and run 'eggpool check-config'.\n"
        )
        return

    # Check if server is already running before starting
    from eggpool.runtime_paths import default_pid_file, read_pid_file

    pid_file = default_pid_file()
    if pid_file.exists():
        pid = read_pid_file(pid_file)
        if pid is not None:
            try:
                os.kill(pid, 0)  # Check if process exists
                sys.stdout.write(
                    "\nServer is already running. "
                    "Use 'eggpool restart' to apply configuration changes.\n"
                )
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass  # Stale PID file, continue to start

    # Start the server
    sys.stdout.write("\n--- Starting Server ---\n")
    os.execvp(  # noqa: S602
        _sys.executable,
        [_sys.executable, "-m", "eggpool", "--config", config_path, "serve"],
    )
