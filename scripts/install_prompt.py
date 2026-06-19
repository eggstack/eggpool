#!/usr/bin/env python3
"""Post-install onboarding prompt for EggPool.

Called at the end of install.sh to ask the user if they want to set up
a provider. This avoids bash stdin issues with 'read' after echo output.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import termios
import tty


def _find_eggpool_dir() -> str:
    """Find the eggpool directory relative to this script.

    The script lives at scripts/install_prompt.py inside the eggpool repo,
    so the repo root is one level up.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(script_dir)

    # Verify this looks like the eggpool repo
    pyproject = os.path.join(repo_root, "pyproject.toml")
    if os.path.isfile(pyproject):
        with open(pyproject, encoding="utf-8") as f:
            if 'name = "eggpool"' in f.read():
                return repo_root

    # Fallback: check common install locations
    for candidate in [
        os.path.expanduser("~/eggpool"),
        os.path.join(os.getcwd(), "eggpool"),
    ]:
        pyproject = os.path.join(candidate, "pyproject.toml")
        if os.path.isfile(pyproject):
            with open(pyproject, encoding="utf-8") as f:
                if 'name = "eggpool"' in f.read():
                    return candidate

    # Last resort: current directory
    return os.getcwd()


def _prompt_yn(message: str) -> bool:
    """Prompt user with a y/n question using raw terminal input.

    Returns True for y/Y, False for everything else.
    Falls back to simple input() if stdin is not a terminal.
    """
    sys.stdout.write(f"{message} (y/n): ")
    sys.stdout.flush()

    # Try raw terminal input first (works in real terminals)
    try:
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        tty.setraw(fd)
    except (termios.error, ValueError, OSError, io.UnsupportedOperation):
        # Not a terminal - fall back to simple input
        try:
            line = sys.stdin.readline().strip().lower()
            return line in ("y", "yes")
        except (EOFError, ValueError):
            return False

    try:
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
            if ch in ("n", "N", "\x03", "\x1b", "\x04"):
                sys.stdout.write("n\r\n")
                sys.stdout.flush()
                return False
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def main() -> None:
    """Run the post-install onboarding prompt."""
    eggpool_dir = _find_eggpool_dir()

    # Ensure we're in the eggpool directory
    os.chdir(eggpool_dir)

    if _prompt_yn("Would you like to set up a provider now?"):
        sys.stdout.write("\nStarting onboarding setup...\n")
        sys.stdout.flush()
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "eggpool", "onboard"],
            cwd=eggpool_dir,
        )
        sys.exit(result.returncode)
    else:
        sys.stdout.write("\nSkipping onboarding. You can run it later with:\n")
        sys.stdout.write("  uv run eggpool onboard\n")
        sys.stdout.flush()
        sys.exit(0)


if __name__ == "__main__":
    main()
