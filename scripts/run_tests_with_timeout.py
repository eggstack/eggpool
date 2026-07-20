"""Run pytest with hard wall-clock timeout that kills the entire process group.

Usage: python scripts/run_tests_with_timeout.py --timeout 60 -- pytest ... [args]

This is safer than pytest's own -p no:cacheprovider patterns because it
terminates the process group if pytest hangs (e.g. waiting on an
asyncio event that never fires).
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import subprocess
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(description="Run pytest with hard kill timeout")
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Wall-clock timeout in seconds (default 120)",
    )
    parser.add_argument("--marker", type=str, default=None, help="Optional test marker")
    parser.add_argument("--junit", type=str, default=None, help="JUnit XML output path")
    parser.add_argument("--label", type=str, default="pytest", help="Label for logs")
    parser.add_argument(
        "--root",
        type=str,
        default="/Users/davidbowman/projects/gorouter",
        help="Repo root directory",
    )
    parser.add_argument("args", nargs=argparse.REMAINDER, help="Extra pytest args")
    opts = parser.parse_args()

    if opts.args and opts.args[0] == "--":
        opts.args = opts.args[1:]

    env = os.environ.copy()
    env["PYTHONHASHSEED"] = "0"
    env["TZ"] = "UTC"

    cmd = ["uv", "run", "pytest"]
    if opts.marker:
        cmd.extend(["-m", opts.marker])
    if opts.junit:
        cmd.extend([f"--junitxml={opts.junit}"])
    cmd.extend(opts.args)

    print(f"[{opts.label}] cmd: {' '.join(cmd)}", flush=True)
    start = time.monotonic()

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=opts.root,
            env=env,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: failed to start subprocess: {exc}", flush=True)
        return 2

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            if time.monotonic() - start > opts.timeout:
                print(
                    f"\n[{opts.label}] TIMEOUT after {opts.timeout}s; killing group",
                    flush=True,
                )
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                return 124
        rc = proc.wait()
    except KeyboardInterrupt:
        print(f"\n[{opts.label}] interrupted; killing group", flush=True)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return 130

    elapsed = time.monotonic() - start
    print(f"\n[{opts.label}] exit={rc} elapsed={elapsed:.1f}s", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
