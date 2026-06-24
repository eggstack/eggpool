#!/usr/bin/env python3
"""Post-install onboarding prompt for EggPool.

Called at the end of install.sh to ask the user if they want to set up
a provider. Uses Python's input() which handles terminal detection.
"""

from __future__ import annotations

import os
import shutil
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


def _resolve_eggpool_cmd(config_path: str) -> tuple[list[str], str]:
    """Pick the invocation that will run the eggpool CLI.

    Prefers the bare ``eggpool`` command installed by ``pipx`` or
    ``uv tool install``. Falls back to ``uv run eggpool`` from the
    repo directory when the bare command isn't on PATH yet (e.g. the
    install script ran but PATH wasn't refreshed in this shell).

    Returns a tuple of (argv_prefix, mode) where ``argv_prefix`` ends
    with ``--config <path>`` (callers append subcommands) and ``mode``
    is one of:
        - ``"global"`` — bare command, no CWD dependence
        - ``"uv-run"`` — fallback via uv from the repo dir

    Raises ``SystemExit`` with an actionable message when neither is
    available.
    """
    if shutil.which("eggpool") is not None:
        return (["eggpool", "--config", config_path], "global")

    if shutil.which("uv") is not None and os.path.isfile(
        os.path.join(_find_eggpool_dir(), "pyproject.toml")
    ):
        repo_dir = _find_eggpool_dir()
        return (
            ["uv", "run", "--directory", repo_dir, "eggpool", "--config", config_path],
            "uv-run",
        )

    msg = (
        "Error: could not find 'eggpool' on PATH and 'uv' is not installed.\n"
        "Restart your shell so the freshly installed 'eggpool' command is on PATH,\n"
        "or re-run scripts/install.sh."
    )
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    """Run the post-install onboarding prompt."""
    eggpool_dir = _find_eggpool_dir()
    config_path = os.path.join(eggpool_dir, "config.toml")

    try:
        answer = (
            input("Would you like to set up a provider now? (y/n): ").strip().lower()
        )
    except (EOFError, KeyboardInterrupt):
        answer = ""

    if answer in ("y", "yes"):
        print("\nStarting onboarding setup...")
        prefix, mode = _resolve_eggpool_cmd(config_path)
        if mode == "uv-run":
            print(
                "  Note: 'eggpool' not on PATH yet — using 'uv run' from the repo dir."
            )
            print("  Restart your shell afterwards for the bare 'eggpool' command.")
        cmd = [*prefix, "onboard"]
        result = subprocess.run(cmd)  # noqa: S603
        sys.exit(result.returncode)
    else:
        print("\nSkipping onboarding. You can run it later with:")
        print(f"  eggpool --config {config_path} onboard")
        sys.exit(0)


if __name__ == "__main__":
    main()
