"""Run the bounded deterministic M10 Q003 database qualification aggregate.

This runner only orchestrates focused evidence.  Compatibility assertions stay
in the Python/Rust tests so the aggregate cannot pass by replacing one side
with a fixture or by normalizing durable differences away.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSON = ROOT / "migration-rs/closure/qualification/003-run.json"
DEFAULT_MARKDOWN = ROOT / "migration-rs/closure/qualification/003-run.md"
RUST_EXECUTABLE = ROOT / "rust/target/debug/eggpool"
MANIFEST_VERSION = "m10-q003.v1"


@dataclass(frozen=True)
class CommandResult:
    """Bounded command outcome; process output is never written to evidence."""

    command_id: str
    argv: tuple[str, ...]
    returncode: int | None
    timed_out: bool
    duration_ms: int
    reason: str

    @property
    def status(self) -> str:
        if self.timed_out or self.returncode is None:
            return "infrastructure-error"
        return "pass" if self.returncode == 0 else "fail"

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.command_id,
            "command": list(self.argv),
            "status": self.status,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "reason": self.reason[:240],
        }


COMMANDS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "q003.black-box",
        "Run Python/Rust upgrade, rollback, repository, WAL, backup, and fault "
        "evidence.",
        (
            "uv",
            "run",
            "pytest",
            "tests/migration_rs/test_q003_database_compatibility.py",
            "-q",
            "--tb=short",
            "--maxfail=1",
        ),
    ),
    (
        "q003.rust-database",
        "Run Rust migration, backup, operator, and database compatibility suites.",
        (
            "cargo",
            "test",
            "--manifest-path",
            "rust/Cargo.toml",
            "--test",
            "operations_o006",
            "--test",
            "operations_o007",
            "--test",
            "database_compatibility",
            "--",
            "--test-threads=1",
        ),
    ),
    (
        "q003.python-oracle",
        "Run the Python backup and migration oracle suites.",
        (
            "uv",
            "run",
            "pytest",
            "tests/unit/test_lifecycle_backup.py",
            "tests/integration/test_migration_compatibility.py",
            "-q",
            "--tb=short",
            "--maxfail=1",
        ),
    ),
    (
        "q003.operations-surface",
        "Retain the complete deterministic operator command surface evidence.",
        (
            "uv",
            "run",
            "pytest",
            "tests/migration_rs/test_o010_operations.py",
            "-q",
            "--tb=short",
            "--maxfail=1",
        ),
    ),
)


def _environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("EGGPOOL_")
        and key
        not in {
            "SERVER_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "EGGPOOL_E2E_OPENCODE_GO_API_KEY",
        }
    }


def _run(command_id: str, argv: Sequence[str], timeout: float) -> CommandResult:
    started = time.monotonic()
    try:
        result = subprocess.run(
            list(argv),
            cwd=ROOT,
            env=_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(
            command_id,
            tuple(argv),
            None,
            True,
            round((time.monotonic() - started) * 1000),
            "command timed out",
        )
    except OSError as error:
        return CommandResult(
            command_id,
            tuple(argv),
            None,
            False,
            round((time.monotonic() - started) * 1000),
            type(error).__name__,
        )
    diagnostic = (result.stderr or result.stdout).decode("utf-8", "replace")
    return CommandResult(
        command_id,
        tuple(argv),
        result.returncode,
        False,
        round((time.monotonic() - started) * 1000),
        " ".join(diagnostic.split())[:240],
    )


def _git_sha() -> str:
    result = _run("git", ("git", "rev-parse", "HEAD"), 10)
    return result.reason if result.returncode == 0 else "unknown"


def run_qualification(
    *, timeout: float = 300.0, skip_build: bool = False
) -> dict[str, object]:
    """Run Q003 and return its bounded machine-readable evidence."""
    started = time.monotonic()
    preflight: list[dict[str, object]] = []
    if RUST_EXECUTABLE.is_file() or skip_build:
        preflight.append(
            {
                "id": "rust-candidate",
                "status": "pass"
                if RUST_EXECUTABLE.is_file()
                else "infrastructure-error",
                "reason": "candidate present"
                if RUST_EXECUTABLE.is_file()
                else "build skipped",
            }
        )
    else:
        build = _run(
            "q003.rust-build",
            ("cargo", "build", "--manifest-path", "rust/Cargo.toml"),
            timeout,
        )
        preflight.append(build.to_dict())

    results: list[CommandResult] = []
    if RUST_EXECUTABLE.is_file():
        for command_id, _description, argv in COMMANDS:
            results.append(_run(command_id, argv, timeout))
    else:
        results = [
            CommandResult(
                command_id,
                argv,
                None,
                False,
                0,
                "Rust candidate unavailable after preflight",
            )
            for command_id, _description, argv in COMMANDS
        ]

    counts = {"pass": 0, "fail": 0, "infrastructure-error": 0}
    for result in results:
        counts[result.status] += 1
    return {
        "candidate_sha": _git_sha(),
        "environment": {
            "os": sys.platform,
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "network_policy": "loopback-only",
        },
        "manifest_version": MANIFEST_VERSION,
        "plan": "Q003",
        "preflight": preflight,
        "commands": [result.to_dict() for result in results],
        "counts": counts,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }


def markdown_report(report: dict[str, object]) -> str:
    commands = cast("list[dict[str, object]]", report["commands"])
    lines = [
        "# Q003 Database Qualification Run",
        "",
        f"Manifest: `{report['manifest_version']}`  ",
        f"Candidate: `{report['candidate_sha']}`  ",
        f"Duration: `{report['duration_ms']} ms`",
        "",
        "| Command | Status | Duration | Reason |",
        "|---|---|---:|---|",
    ]
    for command in commands:
        lines.append(
            f"| `{command['id']}` | `{command['status']}` | "
            f"{command['duration_ms']} ms | {command['reason']} |"
        )
    lines.extend(
        [
            "",
            f"Counts: `{json.dumps(report['counts'], sort_keys=True)}`",
            "",
            "Process output, credentials, database contents, and temporary "
            "paths are omitted.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_report(
    report: dict[str, object], json_path: Path, markdown_path: Path
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(markdown_report(report), encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--skip-build", action="store_true")
    options = parser.parse_args(argv)
    report = run_qualification(timeout=options.timeout, skip_build=options.skip_build)
    write_report(report, options.output, options.markdown)
    print(json.dumps(report["counts"], sort_keys=True))
    return (
        0
        if report["counts"]
        == {"pass": len(COMMANDS), "fail": 0, "infrastructure-error": 0}
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
