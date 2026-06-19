#!/usr/bin/env python3
"""Post-install onboarding prompt for EggPool.

Called at the end of install.sh to ask the user if they want to set up
a provider. Uses Python's input() which handles terminal detection.
"""

from __future__ import annotations

import os
import subprocess
import sys


def _find_eggpool_dir() -> str:
    """Find the eggpool directory relative to this script."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(script_dir)

    pyproject = os.path.join(repo_root, "pyproject.toml")
    if os.path.isfile(pyproject):
        with open(pyproject, encoding="utf-8") as f:
            if 'name = "eggpool"' in f.read():
                return repo_root

    for candidate in [
        os.path.expanduser("~/eggpool"),
        os.path.join(os.getcwd(), "eggpool"),
    ]:
        pyproject = os.path.join(candidate, "pyproject.toml")
        if os.path.isfile(pyproject):
            with open(pyproject, encoding="utf-8") as f:
                if 'name = "eggpool"' in f.read():
                    return candidate

    return os.getcwd()


def main() -> None:
    """Run the post-install onboarding prompt."""
    eggpool_dir = _find_eggpool_dir()
    os.chdir(eggpool_dir)

    try:
        answer = (
            input("Would you like to set up a provider now? (y/n): ").strip().lower()
        )
    except (EOFError, KeyboardInterrupt):
        answer = ""

    if answer in ("y", "yes"):
        print("\nStarting onboarding setup...")
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "eggpool", "onboard"],
            cwd=eggpool_dir,
        )
        sys.exit(result.returncode)
    else:
        print("\nSkipping onboarding. You can run it later with:")
        print("  uv run eggpool onboard")
        sys.exit(0)


if __name__ == "__main__":
    main()
