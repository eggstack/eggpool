"""Run the bounded, deterministic M10 Q002 qualification aggregate.

The runner is deliberately an orchestration layer.  Compatibility logic stays
in the migration harness, focused Python tests, and Rust integration suites;
this module only validates ownership, runs named evidence, and records a
bounded result suitable for review.

Usage::

    uv run python scripts/qualification_runner.py
    uv run python scripts/qualification_runner.py --output /tmp/q002.json

The default run builds the debug Rust candidate when it is absent, then runs
the deterministic migration suites.  No live provider, rootful operation, or
credential is used.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "migration-rs/fixtures/qualification/m10-q001-manifest.json"
DEFAULT_JSON = ROOT / "migration-rs/closure/qualification/002-run.json"
DEFAULT_MARKDOWN = ROOT / "migration-rs/closure/qualification/002-run.md"
DEFAULT_RUST_EXECUTABLE = ROOT / "rust/target/debug/eggpool"
MAX_REASON_LENGTH = 240
MAX_RESULT_BYTES = 256 * 1024


class ResultStatus(StrEnum):
    """Outcome vocabulary used by the aggregate, including infrastructure."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"
    BLOCK = "block"
    INFRASTRUCTURE_ERROR = "infrastructure-error"


class ManifestValidationError(ValueError):
    """Raised when the frozen Q001 contract cannot safely drive Q002."""


class CommandExecutor(Protocol):
    """Small seam used by tests to inject deterministic command outcomes."""

    def __call__(
        self, argv: Sequence[str], *, cwd: Path, timeout: float
    ) -> ProcessResult: ...


@dataclass(frozen=True)
class ProcessResult:
    """Bounded command outcome; stdout/stderr are never part of the artifact."""

    argv: tuple[str, ...]
    returncode: int | None
    timed_out: bool
    duration_ms: int
    diagnostic: str = ""


@dataclass(frozen=True)
class QualificationCell:
    """The small manifest projection needed by the Q002 orchestration layer."""

    cell_id: str
    subsystem: str
    surface: str
    normalization_rule: str
    owner_plan: str


@dataclass(frozen=True)
class QualificationCommand:
    """One named command and the manifest cells it supplies evidence for."""

    command_id: str
    description: str
    argv: tuple[str, ...]
    cell_ids: tuple[str, ...] = ()
    mandatory: bool = True


@dataclass(frozen=True)
class CellResult:
    """A reviewable cell result with structural mismatch fields always present."""

    cell: QualificationCell
    status: ResultStatus
    command_id: str
    command: tuple[str, ...]
    duration_ms: int
    reason_category: str
    reason: str
    python_observation: dict[str, Any] | None = None
    rust_observation: dict[str, Any] | None = None
    first_differing_semantic_field: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_id": self.cell.cell_id,
            "subsystem": self.cell.subsystem,
            "status": self.status.value,
            "command_id": self.command_id,
            "command": list(self.command),
            "duration_ms": self.duration_ms,
            "reason_category": self.reason_category,
            "reason": _bounded_reason(self.reason),
            "normalization_rule": self.cell.normalization_rule,
            "python_observation": self.python_observation,
            "rust_observation": self.rust_observation,
            "first_differing_semantic_field": self.first_differing_semantic_field,
            "owning_subsystem": self.cell.subsystem,
        }


@dataclass(frozen=True)
class RunReport:
    """Complete bounded machine-readable Q002 report."""

    manifest_path: str
    manifest_version: str
    candidate_sha: str
    python_identity: str
    rust_identity: str
    environment: dict[str, str]
    commands: tuple[QualificationCommand, ...]
    command_outcomes: tuple[dict[str, Any], ...]
    results: tuple[CellResult, ...]
    preflight: tuple[dict[str, Any], ...]
    started_at_utc: str
    duration_ms: int

    def to_dict(self) -> dict[str, Any]:
        counts = {status.value: 0 for status in ResultStatus}
        for result in self.results:
            counts[result.status.value] += 1
        return {
            "schema_version": "m10-q002.v1",
            "plan": "Q002",
            "manifest_path": self.manifest_path,
            "manifest_version": self.manifest_version,
            "candidate_sha": self.candidate_sha,
            "python_identity": self.python_identity,
            "rust_identity": self.rust_identity,
            "environment": self.environment,
            "started_at_utc": self.started_at_utc,
            "duration_ms": self.duration_ms,
            "counts": counts,
            "preflight": list(self.preflight),
            "commands": [
                {
                    "id": command.command_id,
                    "description": command.description,
                    "command": list(command.argv),
                    "cell_ids": list(command.cell_ids),
                    "mandatory": command.mandatory,
                    "outcome": next(
                        outcome
                        for outcome in self.command_outcomes
                        if outcome["id"] == command.command_id
                    ),
                }
                for command in self.commands
            ],
            "results": [result.to_dict() for result in self.results],
        }


# This is intentionally checked against the live manifest.  It prevents a
# renamed, removed, or newly assigned cell from silently disappearing from the
# aggregate when a focused test command is edited.
EXPECTED_Q002_CELL_IDS: tuple[str, ...] = (
    "q001.config.resolution",
    "q001.config.provider-forms",
    "q001.config.reload",
    "q001.filesystem.runtime-paths",
    "q001.operations.mutation",
    "q001.api.health-readiness-models",
    "q001.api.chat-finite",
    "q001.api.chat-streaming",
    "q001.api.responses-finite",
    "q001.api.responses-streaming",
    "q001.api.messages-finite",
    "q001.api.messages-streaming",
    "q001.api.limits-errors",
    "q001.provider.cross-surface",
    "q001.routing.model-router",
    "q001.routing.retry-no-replay",
    "q001.runtime.generation",
    "q001.runtime.background",
)


def _bounded_reason(value: str) -> str:
    """Keep diagnostics short and remove common secret-shaped values."""
    sanitized = value.replace("\x00", " ").replace("\n", " ").strip()
    for marker in ("Bearer ", "bearer ", "sk-", "api_key=", "token="):
        if marker in sanitized:
            sanitized = sanitized.split(marker, 1)[0] + "<redacted>"
    return sanitized[:MAX_REASON_LENGTH]


def _manifest_cells(
    manifest: dict[str, Any], root: Path
) -> tuple[QualificationCell, ...]:
    """Validate the frozen manifest and return exactly Q002's owned cells."""
    cell_schema = cast("dict[str, Any]", manifest.get("cell_schema", {}))
    required = set(cast("list[str]", cell_schema.get("required_fields", [])))
    enums = cast("dict[str, Any]", manifest.get("enums", {}))
    cells = cast("list[Any] | None", manifest.get("cells"))
    if not required or not isinstance(cells, list):
        raise ManifestValidationError("Q001 manifest has no valid cell schema")
    valid_classes = set(enums.get("observation_class", []))
    valid_environments = set(enums.get("environment_class", []))
    valid_owners = set(enums.get("owner_plan", []))
    valid_statuses = set(enums.get("closure_status", []))
    normalization_rules = cast(
        "dict[str, Any]", manifest.get("normalization_rules", {})
    )
    ids: set[str] = set()
    selected: list[QualificationCell] = []
    for raw_cell in cells:
        if not isinstance(raw_cell, dict):
            raise ManifestValidationError("Q001 cell is missing required fields")
        cell = cast("dict[str, Any]", raw_cell)
        if not required <= cell.keys():
            raise ManifestValidationError("Q001 cell is missing required fields")
        cell_id = cell["id"]
        if not isinstance(cell_id, str) or cell_id in ids:
            raise ManifestValidationError(f"duplicate or invalid cell id: {cell_id!r}")
        ids.add(cell_id)
        if cell["observation_class"] not in valid_classes:
            raise ManifestValidationError(f"invalid observation class: {cell_id}")
        if cell["environment_class"] not in valid_environments:
            raise ManifestValidationError(f"invalid environment class: {cell_id}")
        if cell["owner_plan"] is not None and cell["owner_plan"] not in valid_owners:
            raise ManifestValidationError(f"invalid owner plan: {cell_id}")
        if cell["closure_status"] not in valid_statuses:
            raise ManifestValidationError(f"invalid closure status: {cell_id}")
        rule = cell["normalization_rule"]
        if rule not in normalization_rules:
            raise ManifestValidationError(f"unknown normalization rule: {rule}")
        for evidence in cell["existing_evidence"]:
            if not isinstance(evidence, str) or not (root / evidence).exists():
                raise ManifestValidationError(f"missing evidence for {cell_id}")
        if (
            cell["owner_plan"] == "Q002"
            and cell["environment_class"] == "deterministic-local"
        ):
            selected.append(
                QualificationCell(
                    cell_id,
                    str(cell["subsystem"]),
                    str(cell["surface"]),
                    str(rule),
                    "Q002",
                )
            )
    expected = set(EXPECTED_Q002_CELL_IDS)
    actual = {cell.cell_id for cell in selected}
    if actual != expected:
        missing = ", ".join(sorted(expected - actual)) or "none"
        stale = ", ".join(sorted(actual - expected)) or "none"
        raise ManifestValidationError(
            f"Q002 deterministic ownership drift (missing={missing}; stale={stale})"
        )
    return tuple(sorted(selected, key=lambda cell: cell.cell_id))


def load_q002_cells(
    manifest_path: Path = DEFAULT_MANIFEST,
    *,
    root: Path = ROOT,
) -> tuple[QualificationCell, ...]:
    """Load and fail closed on the Q001 manifest used by the aggregate."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestValidationError(
            f"cannot read Q001 manifest: {manifest_path}"
        ) from error
    if not isinstance(manifest, dict):
        raise ManifestValidationError("Q001 manifest must be an object")
    manifest = cast("dict[str, Any]", manifest)
    if manifest.get("status") != "frozen":
        raise ManifestValidationError("Q001 manifest is not frozen")
    return _manifest_cells(manifest, root)


def _pytest(*paths: str) -> tuple[str, ...]:
    return ("uv", "run", "pytest", *paths, "-q", "--tb=short", "--maxfail=1")


def _cargo(*args: str) -> tuple[str, ...]:
    return ("cargo", *args, "--", "--test-threads=1")


def build_commands() -> tuple[QualificationCommand, ...]:
    """Return the named, reproducible evidence commands for Q002."""
    return (
        QualificationCommand(
            "q002.manifest",
            "Validate the frozen Q001 manifest and normalization registry.",
            _pytest("tests/migration_rs/test_q001_manifest.py"),
        ),
        QualificationCommand(
            "q002.config-operations",
            "Run config, filesystem, mutation, and CLI differential evidence.",
            _pytest(
                "tests/migration_rs/test_f003_config_cli.py",
                "tests/migration_rs/test_o001_operations.py",
                "tests/migration_rs/test_r001_runtime_lifecycle.py",
            ),
            (
                "q001.config.resolution",
                "q001.config.reload",
                "q001.filesystem.runtime-paths",
                "q001.operations.mutation",
            ),
        ),
        QualificationCommand(
            "q002.server-lifecycle",
            "Run real black-box lifecycle, health, and local-provider scenarios.",
            _pytest(
                "tests/migration_rs/test_f005_server.py",
                "tests/migration_rs/test_f006_safety.py",
                "tests/migration_rs/test_q002_cross_boundary.py",
            ),
            (
                "q001.api.health-readiness-models",
                "q001.api.chat-finite",
                "q001.api.chat-streaming",
            ),
        ),
        QualificationCommand(
            "q002.provider-wire",
            "Run provider transport and cross-surface wire qualification.",
            _pytest(
                "tests/migration_rs/test_t001_provider_transport.py",
                "tests/migration_rs/test_w012_canonical_wire.py",
            ),
            (
                "q001.config.provider-forms",
                "q001.provider.cross-surface",
                "q001.api.responses-finite",
                "q001.api.responses-streaming",
                "q001.api.messages-finite",
                "q001.api.messages-streaming",
            ),
        ),
        QualificationCommand(
            "q002-routing-coordinator",
            "Run Rust coordinator, retry, routing, and model-router suites.",
            _cargo(
                "test",
                "--manifest-path",
                "rust/Cargo.toml",
                "--test",
                "coordinator_c009",
                "--test",
                "coordinator_c013",
                "--test",
                "canonical_request",
                "--test",
                "model_router",
            ),
            (
                "q001.routing.model-router",
                "q001.routing.retry-no-replay",
                "q001.api.limits-errors",
            ),
        ),
        QualificationCommand(
            "q002.runtime-recovery",
            "Run runtime generation, recovery, and durable-operation suites.",
            _cargo(
                "test",
                "--manifest-path",
                "rust/Cargo.toml",
                "--test",
                "runtime_lifecycle_r003",
                "--test",
                "runtime_lifecycle_r004",
                "--test",
                "runtime_lifecycle_r006",
                "--test",
                "runtime_lifecycle_r008",
                "--test",
                "runtime_lifecycle_r009",
            ),
            ("q001.runtime.generation", "q001.runtime.background"),
        ),
    )


def _default_executor(
    argv: Sequence[str], *, cwd: Path, timeout: float
) -> ProcessResult:
    started = time.monotonic()
    try:
        process = subprocess.run(
            list(argv),
            cwd=cwd,
            env=_sanitized_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        diagnostic = str(error).splitlines()[0] if str(error) else "command timed out"
        return ProcessResult(tuple(argv), None, True, _elapsed_ms(started), diagnostic)
    except (OSError, ValueError) as error:
        return ProcessResult(tuple(argv), None, False, _elapsed_ms(started), str(error))
    diagnostic = (process.stderr or process.stdout).decode("utf-8", "replace")
    return ProcessResult(
        tuple(argv),
        process.returncode,
        False,
        _elapsed_ms(started),
        _bounded_reason(diagnostic),
    )


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _sanitized_environment() -> dict[str, str]:
    """Keep credentials and unrelated host environment out of child runs."""
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


def _status_for_process(result: ProcessResult) -> tuple[ResultStatus, str, str]:
    if result.timed_out:
        return ResultStatus.INFRASTRUCTURE_ERROR, "infrastructure", "command timed out"
    if result.returncode is None:
        return ResultStatus.INFRASTRUCTURE_ERROR, "infrastructure", result.diagnostic
    if result.returncode == 0:
        return ResultStatus.PASS, "none", ""
    if result.returncode == 5:
        return ResultStatus.SKIP, "manual-review", "no tests were collected"
    return ResultStatus.FAIL, "assertion", result.diagnostic or "command failed"


def _git_sha(root: Path) -> str:
    result = _default_executor(("git", "rev-parse", "HEAD"), cwd=root, timeout=5)
    if result.returncode != 0 or result.timed_out:
        return "unknown"
    return result.diagnostic.strip() or "unknown"


def _environment_metadata() -> dict[str, str]:
    return {
        "os": sys.platform,
        "architecture": os.uname().machine if hasattr(os, "uname") else "unknown",
        "python": (
            f"{sys.version_info.major}.{sys.version_info.minor}."
            f"{sys.version_info.micro}"
        ),
        "network_policy": "loopback-only",
    }


def _preflight(
    root: Path, rust_executable: Path, executor: CommandExecutor
) -> list[dict[str, Any]]:
    if rust_executable.is_file():
        return [
            {"id": "rust-candidate", "status": "pass", "reason": "candidate present"}
        ]
    result = executor(
        ("cargo", "build", "--manifest-path", "rust/Cargo.toml"),
        cwd=root,
        timeout=300,
    )
    status, category, reason = _status_for_process(result)
    if status is ResultStatus.PASS and rust_executable.is_file():
        return [
            {"id": "rust-build", "status": "pass", "duration_ms": result.duration_ms}
        ]
    return [
        {
            "id": "rust-build",
            "status": ResultStatus.INFRASTRUCTURE_ERROR.value,
            "reason_category": category,
            "reason": _bounded_reason(reason or "Rust candidate was not produced"),
            "duration_ms": result.duration_ms,
        }
    ]


def run_qualification(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    root: Path = ROOT,
    rust_executable: Path = DEFAULT_RUST_EXECUTABLE,
    timeout: float = 300.0,
    executor: CommandExecutor = _default_executor,
    skip_build: bool = False,
) -> RunReport:
    """Run Q002 and return a report; manifest errors fail before commands run."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ManifestValidationError("Q001 manifest must be an object")
    manifest = cast("dict[str, Any]", manifest)
    if manifest.get("status") != "frozen":
        raise ManifestValidationError("Q001 manifest is not frozen")
    cells = _manifest_cells(manifest, root)
    cell_by_id = {cell.cell_id: cell for cell in cells}
    commands = build_commands()
    command_cell_ids = {cell_id for command in commands for cell_id in command.cell_ids}
    if command_cell_ids != set(cell_by_id):
        missing = set(cell_by_id) - command_cell_ids
        extra = command_cell_ids - set(cell_by_id)
        raise ManifestValidationError(
            "Q002 command ownership drift "
            f"(missing={sorted(missing)}; extra={sorted(extra)})"
        )
    started = time.monotonic()
    if skip_build:
        preflight = (
            [
                {
                    "id": "rust-candidate",
                    "status": "pass",
                    "reason": "candidate present",
                }
            ]
            if rust_executable.is_file()
            else [
                {
                    "id": "rust-candidate",
                    "status": ResultStatus.INFRASTRUCTURE_ERROR.value,
                    "reason": "candidate absent and build skipped",
                }
            ]
        )
    else:
        preflight = _preflight(root, rust_executable, executor)
    rust_ready = any(item.get("status") == "pass" for item in preflight)
    results: list[CellResult] = []
    command_outcomes: list[dict[str, Any]] = []
    for command in commands:
        if command.command_id == "q002.manifest":
            process = executor(command.argv, cwd=root, timeout=timeout)
        elif not rust_ready:
            process = ProcessResult(
                command.argv,
                None,
                False,
                0,
                "Rust candidate unavailable after preflight",
            )
            status, category, reason = (
                ResultStatus.BLOCK,
                "infrastructure",
                process.diagnostic,
            )
            command_outcomes.append(
                {
                    "id": command.command_id,
                    "status": status.value,
                    "duration_ms": process.duration_ms,
                    "reason_category": category,
                    "reason": _bounded_reason(reason),
                }
            )
            for cell_id in command.cell_ids:
                results.append(
                    CellResult(
                        cell_by_id[cell_id],
                        status,
                        command.command_id,
                        command.argv,
                        process.duration_ms,
                        category,
                        reason,
                    )
                )
            continue
        else:
            process = executor(command.argv, cwd=root, timeout=timeout)
        status, category, reason = _status_for_process(process)
        command_outcomes.append(
            {
                "id": command.command_id,
                "status": status.value,
                "duration_ms": process.duration_ms,
                "reason_category": category,
                "reason": _bounded_reason(reason),
            }
        )
        for cell_id in command.cell_ids:
            results.append(
                CellResult(
                    cell_by_id[cell_id],
                    status,
                    command.command_id,
                    command.argv,
                    process.duration_ms,
                    category,
                    reason,
                )
            )
    return RunReport(
        manifest_path=str(manifest_path.relative_to(root))
        if manifest_path.is_relative_to(root)
        else str(manifest_path),
        manifest_version=str(manifest["manifest_version"]),
        candidate_sha=_git_sha(root),
        python_identity=sys.executable,
        rust_identity=str(rust_executable),
        environment=_environment_metadata(),
        commands=commands,
        command_outcomes=tuple(command_outcomes),
        results=tuple(results),
        preflight=tuple(preflight),
        started_at_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        duration_ms=_elapsed_ms(started),
    )


def report_markdown(report: RunReport) -> str:
    """Render a compact human-readable table without command output bodies."""
    data = report.to_dict()
    lines = [
        "# Q002 Deterministic Qualification Run",
        "",
        f"Manifest: `{data['manifest_version']}`  ",
        f"Candidate: `{data['candidate_sha']}`  ",
        f"Duration: `{data['duration_ms']} ms`",
        "",
        "| Cell | Status | Command | Normalization | Reason |",
        "|---|---|---|---|---|",
    ]
    for result in report.results:
        lines.append(
            (
                "| `{cell_id}` | `{status}` | `{command_id}` | "
                "`{normalization_rule}` | {reason} |"
            ).format(**result.to_dict())
        )
    lines.extend(
        [
            "",
            "Counts: "
            + ", ".join(
                f"{key}={value}" for key, value in data["counts"].items() if value
            ),
            "",
            "Commands are recorded as argv arrays; provider bodies, credentials, "
            "and full process output are omitted.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_report(report: RunReport, json_path: Path, markdown_path: Path) -> None:
    """Write bounded JSON/Markdown artifacts atomically enough for local use."""
    payload = json.dumps(report.to_dict(), sort_keys=True, separators=(",", ":"))
    if len(payload.encode("utf-8")) > MAX_RESULT_BYTES:
        raise ValueError("Q002 result artifact exceeds its bounded size")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(payload + "\n", encoding="utf-8")
    markdown_path.write_text(report_markdown(report), encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--rust-executable", type=Path, default=DEFAULT_RUST_EXECUTABLE)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--skip-build", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point with fail-closed manifest errors."""
    options = _parser().parse_args(argv)
    try:
        report = run_qualification(
            manifest_path=options.manifest,
            root=ROOT,
            rust_executable=options.rust_executable,
            timeout=options.timeout,
            skip_build=options.skip_build,
        )
        write_report(report, options.output, options.markdown)
    except (
        ManifestValidationError,
        OSError,
        json.JSONDecodeError,
        ValueError,
    ) as error:
        print(
            f"Q002 infrastructure-error: {_bounded_reason(str(error))}", file=sys.stderr
        )
        return 2
    print(json.dumps(report.to_dict()["counts"], sort_keys=True))
    commands_passed = all(
        outcome["status"] == ResultStatus.PASS.value
        for outcome in report.command_outcomes
    )
    cells_passed = all(result.status is ResultStatus.PASS for result in report.results)
    return 0 if commands_passed and cells_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
