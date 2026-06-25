"""Import-budget regression test for :mod:`eggpool.fastcli`.

Running ``eggpool croncheck`` and ``eggpool ensure-running`` (when the
server is already alive) must stay close to stdlib-only. We assert this
by spawning a fresh interpreter with ``-X importtime`` and inspecting
which modules the bootstrap actually loaded.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

FORBIDDEN_MODULES: tuple[str, ...] = (
    "fastapi",
    "granian",
    "httpx",
    "pydantic",
    "aiosqlite",
    "eggpool.app",
    "eggpool.db",
    "eggpool.providers",
    "eggpool.models.config",
    "eggpool.lifecycle",
)


def _run_croncheck_subprocess(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    pid_file = tmp_path / "eggpool.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    env = os.environ.copy()
    env["EGGPOOL_PID_FILE"] = str(pid_file)
    env["HOME"] = str(tmp_path)
    env.pop("XDG_RUNTIME_DIR", None)
    return subprocess.run(
        [sys.executable, "-X", "importtime", "-m", "eggpool", "croncheck"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def test_croncheck_subprocess_does_not_import_heavy_modules(tmp_path: Path) -> None:
    result = _run_croncheck_subprocess(tmp_path)
    assert result.returncode == 0, (
        f"croncheck exited {result.returncode}\nstdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )

    forbidden_in_lines: dict[str, list[str]] = {mod: [] for mod in FORBIDDEN_MODULES}
    for line in result.stderr.splitlines():
        for mod in FORBIDDEN_MODULES:
            if mod in line:
                forbidden_in_lines[mod].append(line)

    offenders = {mod: lines for mod, lines in forbidden_in_lines.items() if lines}
    assert not offenders, (
        "fast-path croncheck imported forbidden modules: "
        + ", ".join(f"{mod} ({len(lines)}x)" for mod, lines in offenders.items())
    )
