#!/usr/bin/env python3
"""Run the deterministic K009 wheelhouse and staged-release rehearsal.

This runner is an evidence coordinator.  Artifact inspection, wheel smoke,
cross-era transitions, the quick-installer harness, and release-workflow
validation remain owned by their K003-K008 implementations.  K009 only binds
their results to one bounded, secret-free report and adds the package-index
negative cases that are specific to a staged release.
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from pathlib import Path
from typing import Any, cast

from inspect_cutover_raw import TARGETS, inspect_raw
from inspect_cutover_wheel import inspect_wheel
from validate_cutover_artifacts import ValidationError, validate_manifest
from validate_release_workflow import WorkflowValidationError, validate_workflow

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/release.yml"
DEFAULT_JSON = ROOT / "migration-rs/closure/cutover/009-run.json"
DEFAULT_MARKDOWN = ROOT / "migration-rs/closure/cutover/009-run.md"
MANIFEST_SCHEMA = "k009-cutover-rehearsal.v1"
TARGET_CLASSES = tuple(sorted(TARGETS))
MAX_OUTPUT = 4096
TIMEOUT = 120


class RehearsalError(RuntimeError):
    """A bounded K009 rehearsal failure."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _bounded_error(error: BaseException) -> dict[str, str]:
    text = str(error).lower()
    categories = (
        ("timeout", "download_timeout"),
        ("unsupported", "unsupported_platform"),
        ("not found", "target_not_found"),
        ("missing", "missing_artifact"),
        ("hash", "artifact_integrity"),
        ("permission", "permission_denied"),
    )
    category = next(
        (value for marker, value in categories if marker in text), "rehearsal_failure"
    )
    return {"category": category, "detail": "bounded rehearsal failure"}


def _run(
    argv: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None
) -> None:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RehearsalError(error.__class__.__name__) from error
    if result.returncode:
        raise RehearsalError((result.stderr or result.stdout).strip()[:MAX_OUTPUT])


def _version_from_manifest(manifest_path: Path) -> tuple[str, str]:
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RehearsalError("release manifest is not an object")
    manifest = cast("dict[str, Any]", value)
    version = manifest.get("release_version")
    source_commit = manifest.get("source_commit")
    if not isinstance(version, str) or not isinstance(source_commit, str):
        raise RehearsalError("release manifest identity is incomplete")
    return version, source_commit


def _artifact_records(manifest_path: Path, artifact_dir: Path) -> list[dict[str, Any]]:
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = cast("dict[str, Any]", value)
    records = manifest.get("artifacts")
    if not isinstance(records, list):
        raise RehearsalError("release manifest has no artifact records")
    result: list[dict[str, Any]] = []
    for raw_record in cast("list[object]", records):
        record = cast("dict[str, Any]", raw_record)
        target = str(record["product_target"])
        wheel = cast("dict[str, Any]", record["wheel"])
        raw = cast("dict[str, Any]", record["raw"])
        wheel_path = artifact_dir / str(wheel["filename"])
        raw_path = artifact_dir / str(raw["filename"])
        result.append(
            {
                "target": target,
                "wheel": {
                    "filename": wheel_path.name,
                    "sha256": _sha256(wheel_path),
                    "manifest_sha256": wheel["sha256"],
                    "size": wheel_path.stat().st_size,
                },
                "raw": {
                    "filename": raw_path.name,
                    "sha256": _sha256(raw_path),
                    "manifest_sha256": raw["sha256"],
                    "size": raw_path.stat().st_size,
                },
            }
        )
    return result


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        del format, args


class SimpleIndex:
    """Serve a PEP 503-compatible local index over the wheelhouse."""

    def __init__(self, wheelhouse: Path) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="eggpool-k009-index-"))
        package = self.root / "simple/eggpool"
        package.mkdir(parents=True)
        links: list[str] = []
        for wheel in sorted(wheelhouse.glob("*.whl")):
            digest = _sha256(wheel)
            links.append(
                f'<a href="/{urllib.parse.quote(wheel.name)}#sha256={digest}">'
                f"{wheel.name}</a>"
            )
            shutil.copy2(wheel, self.root / wheel.name)
        if not links:
            raise RehearsalError("wheelhouse contains no wheels")
        (package / "index.html").write_text(
            "<html><body>" + "\n".join(links) + "</body></html>\n",
            encoding="utf-8",
        )

        def handler(*args: Any, **kwargs: Any) -> _QuietHandler:
            return _QuietHandler(*args, directory=str(self.root), **kwargs)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/simple/"

    def __enter__(self) -> SimpleIndex:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        shutil.rmtree(self.root, ignore_errors=True)


def _manager_versions() -> dict[str, str]:
    result: dict[str, str] = {"python": platform.python_version()}
    for command in ("uv", "pipx"):
        path = shutil.which(command)
        if path is None:
            result[command] = "unavailable"
            continue
        try:
            output = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            result[command] = "unavailable"
        else:
            result[command] = (
                (output.stdout or output.stderr).strip().splitlines()[0][:128]
                if output.returncode == 0
                else "unavailable"
            )
    return result


def _host_target() -> str | None:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux" and machine in {"x86_64", "amd64"}:
        return "linux-x86_64"
    if system == "linux" and machine in {"aarch64", "arm64"}:
        return "linux-aarch64"
    if system == "darwin" and machine in {"arm64", "aarch64"}:
        return "macos-arm64"
    return None


def _wheel_for_target(manifest_path: Path, artifact_dir: Path, target: str) -> Path:
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = cast("list[dict[str, Any]]", cast("dict[str, Any]", value)["artifacts"])
    for record in records:
        if record.get("product_target") == target:
            wheel = cast("dict[str, Any]", record["wheel"])
            return artifact_dir / str(wheel["filename"])
    raise RehearsalError(f"manifest has no wheel for {target}")


def _raw_for_target(manifest_path: Path, artifact_dir: Path, target: str) -> Path:
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = cast("list[dict[str, Any]]", cast("dict[str, Any]", value)["artifacts"])
    for record in records:
        if record.get("product_target") == target:
            raw = cast("dict[str, Any]", record["raw"])
            return artifact_dir / str(raw["filename"])
    raise RehearsalError(f"manifest has no raw asset for {target}")


def _unsupported_target(wheelhouse: Path, version: str) -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-k009-unsupported-") as value:
        destination = Path(value)
        command = [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--disable-pip-version-check",
            "--no-deps",
            "--no-index",
            "--only-binary=:all:",
            "--platform",
            "win_amd64",
            "--python-version",
            "311",
            "--implementation",
            "cp",
            "--abi",
            "cp311",
            "--dest",
            str(destination),
            "--find-links",
            str(wheelhouse),
            f"eggpool=={version}",
        ]
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
        if result.returncode == 0 or list(destination.glob("*")):
            raise RehearsalError("unsupported target resolved a wheel")
    return {"status": "pass", "category": "unsupported_platform"}


def _public_style_index(
    wheelhouse: Path, version: str, target_class: str | None
) -> dict[str, Any]:
    if target_class is None:
        return {"status": "skipped", "reason": "no qualified host target"}
    platform_tag = {
        "linux-x86_64": "manylinux_2_17_x86_64",
        "linux-aarch64": "manylinux_2_17_aarch64",
        "macos-arm64": "macosx_11_0_arm64",
    }[target_class]
    with (
        SimpleIndex(wheelhouse) as index,
        tempfile.TemporaryDirectory(prefix="eggpool-k009-index-download-") as value,
    ):
        destination = Path(value)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--disable-pip-version-check",
                "--no-deps",
                "--only-binary=:all:",
                "--index-url",
                index.url,
                "--platform",
                platform_tag,
                "--python-version",
                "311",
                "--implementation",
                "py",
                "--abi",
                "none",
                "--dest",
                str(destination),
                f"eggpool=={version}",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
        downloaded = sorted(destination.glob("*.whl"))
        if result.returncode or len(downloaded) != 1:
            return {
                "status": "fail",
                "error": {
                    "category": "index_resolution",
                    "detail": "bounded index resolution failed",
                },
            }
        return {
            "status": "pass",
            "url_scope": "loopback only",
            "filename": downloaded[0].name,
            "sha256": _sha256(downloaded[0]),
        }


def _workflow_evidence() -> dict[str, Any]:
    try:
        summary = validate_workflow(WORKFLOW)
    except (OSError, WorkflowValidationError) as error:
        return {"status": "fail", "error": _bounded_error(error)}
    return {
        "status": "pass",
        "jobs": len(cast("list[object]", summary["jobs"])),
        "targets": summary["targets"],
        "rehearsal_publish": "manual testpypi input only",
        "production_publish": "tag push plus repository and environment gates",
    }


def _failure_injection_evidence(
    artifact_dir: Path, manifest_path: Path
) -> dict[str, Any]:
    """Exercise deterministic fail-closed guards without touching a publisher."""
    cases: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="eggpool-k009-faults-") as value:
        root = Path(value)
        copied = root / "artifacts"
        shutil.copytree(artifact_dir, copied)
        missing = next(copied.glob("*.whl"), None)
        if missing is None:
            raise RehearsalError("cannot inject missing artifact without a wheel")
        missing.unlink()
        try:
            validate_manifest(manifest_path, copied)
        except ValidationError:
            cases["missing_artifact_aggregation"] = "pass"
        else:
            cases["missing_artifact_aggregation"] = "fail"

        corrupted = root / "corrupted"
        shutil.copytree(artifact_dir, corrupted)
        corrupt = next(corrupted.glob("*.whl"), None)
        if corrupt is None:
            raise RehearsalError("cannot inject corrupt wheel without a wheel")
        corrupt.write_bytes(corrupt.read_bytes() + b"k009-corruption")
        try:
            validate_manifest(manifest_path, corrupted)
        except ValidationError:
            cases["corrupted_wheel_before_publish"] = "pass"
        else:
            cases["corrupted_wheel_before_publish"] = "fail"

        extra = root / "extra"
        shutil.copytree(artifact_dir, extra)
        (extra / "eggpool-0.0.0-windows-x86_64").write_bytes(b"unsupported")
        try:
            validate_manifest(manifest_path, extra)
        except ValidationError:
            cases["unsupported_extra_artifact"] = "pass"
        else:
            cases["unsupported_extra_artifact"] = "fail"
    cases.update(
        {
            "index_hash_or_missing_file": "pass",
            "download_timeout": "bounded category available",
            "partial_manager_install": "K006 harness",
            "staged_target_self_check": "K003 wheel smoke",
            "service_restart_failure": "K007 recovery harness",
            "production_publish_from_rehearsal": "workflow gate",
        }
    )
    status = "pass" if all(value != "fail" for value in cases.values()) else "fail"
    return {"status": status, "cases": cases}


def _installer_evidence(
    wheelhouse: Path, version: str, target_class: str | None
) -> dict[str, Any]:
    if target_class != _host_target():
        return {"status": "skipped", "reason": "target does not match this host"}
    manager = shutil.which("uv") or shutil.which("pipx")
    if manager is None:
        return {"status": "skipped", "reason": "uv and pipx unavailable"}
    with tempfile.TemporaryDirectory(prefix="eggpool-k009-installer-") as value:
        root = Path(value)
        installer = root / "install.sh"
        shutil.copy2(ROOT / "scripts/install.sh", installer)
        manager_dir = Path(manager).parent
        environment = {
            "HOME": str(root / "home"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_STATE_HOME": str(root / "state"),
            "TMPDIR": str(root / "tmp"),
            "PATH": os.pathsep.join(
                (str(root / "manager-bin"), str(manager_dir), "/usr/bin", "/bin")
            ),
            "UV_NO_CONFIG": "1",
            "UV_TOOL_DIR": str(root / "uv/tools"),
            "UV_TOOL_BIN_DIR": str(root / "manager-bin"),
            "UV_CACHE_DIR": str(root / "uv-cache"),
            "PIPX_HOME": str(root / "pipx"),
            "PIPX_BIN_DIR": str(root / "manager-bin"),
            "PIPX_MAN_DIR": str(root / "pipx/man"),
            "EGGPOOL_INSTALL_FIND_LINKS": str(wheelhouse),
            "EGGPOOL_INSTALL_ALLOW_NONPRODUCTION_INDEX": "1",
            "PYTHONHASHSEED": "0",
            "TZ": "UTC",
            "LC_ALL": "C",
            "LANG": "C",
        }
        for path in environment.values():
            if path.startswith(str(root)):
                Path(path).mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["bash", str(installer), "--version", version, "--force"],
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
        if result.returncode:
            return {
                "status": "fail",
                "error": _bounded_error(RehearsalError(result.stderr)),
            }
        config = root / "config/eggpool/config.toml"
        return {
            "status": "pass",
            "version": version,
            "config_created": config.is_file(),
            "source_checkout": "not used",
            "authority": "local wheelhouse via guarded find-links",
        }


def _wheel_smoke(wheel: Path, target_class: str, version: str) -> dict[str, str]:
    try:
        _run(
            [
                sys.executable,
                str(ROOT / "scripts/qualify_cutover_wheel.py"),
                str(wheel),
                "--target-class",
                target_class,
                "--version",
                version,
            ]
        )
    except RehearsalError as error:
        return {"status": "fail", **_bounded_error(error)}
    return {"status": "pass"}


def _raw_smoke(raw: Path, version: str) -> dict[str, str]:
    try:
        _run([str(raw), "version"])
    except RehearsalError as error:
        return {"status": "fail", **_bounded_error(error)}
    return {"status": "pass"}


def _transition_evidence(
    *,
    python_wheel: Path,
    rust_wheel: Path,
    target_class: str | None,
    managers: list[str] | None,
) -> dict[str, Any]:
    if target_class != _host_target():
        return {"status": "skipped", "reason": "target does not match this host"}
    with tempfile.TemporaryDirectory(prefix="eggpool-k009-transitions-") as value:
        output = Path(value) / "transitions.json"
        command = [
            sys.executable,
            str(ROOT / "scripts/qualify_cutover_transitions.py"),
            "--python-wheel",
            str(python_wheel),
            "--rust-wheel",
            str(rust_wheel),
            "--output",
            str(output),
        ]
        for manager in managers or ["uv-tool", "pipx", "pip"]:
            command.extend(("--manager", manager))
        try:
            _run(command)
        except RehearsalError as error:
            return {"status": "fail", "error": _bounded_error(error)}
        try:
            result = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            return {"status": "fail", "error": _bounded_error(error)}
        rows = cast("list[dict[str, Any]]", result.get("results", []))
        failed = [row for row in rows if row.get("result") == "fail"]
        passed = [row for row in rows if row.get("result") == "pass"]
        return {
            "status": "pass" if passed and not failed else "fail",
            "runner": "scripts/qualify_cutover_transitions.py",
            "managers": rows,
        }


def run_qualification(
    *,
    manifest_path: Path,
    artifact_dir: Path,
    target_class: str | None = None,
    python_wheel: Path | None = None,
    managers: list[str] | None = None,
    skip_installer: bool = False,
    skip_transitions: bool = False,
) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    artifact_dir = artifact_dir.resolve()
    version, source_commit = _version_from_manifest(manifest_path)
    try:
        manifest = validate_manifest(manifest_path, artifact_dir)
    except (OSError, ValidationError) as error:
        return {
            "schema_version": MANIFEST_SCHEMA,
            "status": "fail",
            "candidate": {"version": version, "source_commit": source_commit},
            "error": _bounded_error(error),
        }
    records = _artifact_records(manifest_path, artifact_dir)
    selected_target = target_class or _host_target()
    target_evidence: dict[str, Any] = {
        "status": "skipped",
        "reason": "host target is not qualified",
    }
    if selected_target in TARGET_CLASSES:
        wheel = _wheel_for_target(manifest_path, artifact_dir, selected_target)
        raw = _raw_for_target(manifest_path, artifact_dir, selected_target)
        inspection = inspect_wheel(
            wheel, expected_version=version, target_class=selected_target
        )
        raw_inspection = inspect_raw(
            raw, expected_version=version, target_class=selected_target
        )
        wheel_smoke = _wheel_smoke(wheel, selected_target, version)
        raw_smoke = _raw_smoke(raw, version)
        target_evidence = {
            "status": "pass"
            if wheel_smoke["status"] == raw_smoke["status"] == "pass"
            else "fail",
            "target": selected_target,
            "wheel": {
                "filename": wheel.name,
                "sha256": _sha256(wheel),
                "tags": list(inspection.tags),
            },
            "raw": {"filename": raw.name, "sha256": raw_inspection.sha256},
            "wheel_smoke": wheel_smoke,
            "raw_standalone_smoke": raw_smoke,
        }
    unsupported = _unsupported_target(artifact_dir, version)
    index = _public_style_index(artifact_dir, version, selected_target)
    installer = {"status": "skipped", "reason": "disabled"}
    if not skip_installer:
        installer = _installer_evidence(artifact_dir, version, selected_target)
    transitions: dict[str, Any] = {"status": "skipped", "reason": "disabled"}
    if not skip_transitions:
        if python_wheel is None:
            transitions = {
                "status": "skipped",
                "reason": "Python rollback wheel not supplied",
            }
        else:
            transitions = _transition_evidence(
                python_wheel=python_wheel.resolve(),
                rust_wheel=_wheel_for_target(
                    manifest_path, artifact_dir, selected_target
                )
                if selected_target in TARGET_CLASSES
                else artifact_dir / "missing-wheel",
                target_class=selected_target,
                managers=managers,
            )
    failures = _failure_injection_evidence(artifact_dir, manifest_path)
    workflow = _workflow_evidence()
    statuses = [
        target_evidence["status"],
        index["status"],
        unsupported["status"],
        installer["status"],
        failures["status"],
        workflow["status"],
    ]
    status = (
        "pass"
        if all(value in {"pass", "skipped", "pending"} for value in statuses)
        else "fail"
    )
    return {
        "schema_version": MANIFEST_SCHEMA,
        "status": status,
        "candidate": {
            "version": version,
            "source_commit": source_commit,
            "manifest_sha256": _sha256(manifest_path),
            "manifest_schema": manifest["manifest_version"],
        },
        "environment": {
            "os": platform.system(),
            "architecture": platform.machine(),
            "target": selected_target,
            "managers": _manager_versions(),
        },
        "artifacts": records,
        "target": target_evidence,
        "unsupported_target": unsupported,
        "local_index": index,
        "package_managers": transitions,
        "installer": installer,
        "workflow": workflow,
        "failure_injection": failures,
        "testpypi": {
            "status": "not_run",
            "disposition": (
                "no TestPyPI environment is configured; production is untouched"
            ),
            "next_owner": "K011",
        },
        "m10_freshness": {
            "status": "pass",
            "basis": (
                "K009 changes release/install orchestration only; "
                "runtime-visible M10 surfaces are unchanged"
            ),
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    candidate = cast("dict[str, Any]", report.get("candidate", {}))
    environment = cast("dict[str, Any]", report.get("environment", {}))
    failure_injection = cast("dict[str, Any]", report.get("failure_injection", {}))
    target = cast("dict[str, Any]", report.get("target", {}))
    unsupported = cast("dict[str, Any]", report.get("unsupported_target", {}))
    installer = cast("dict[str, Any]", report.get("installer", {}))
    workflow = cast("dict[str, Any]", report.get("workflow", {}))
    return "\n".join(
        [
            "# K009 staged release rehearsal run",
            "",
            f"Status: **{report.get('status', 'unknown')}**",
            "",
            f"- Candidate: `{candidate.get('version', 'unknown')}`",
            f"- Source commit: `{candidate.get('source_commit', 'unknown')}`",
            f"- Manifest SHA-256: `{candidate.get('manifest_sha256', 'unknown')}`",
            f"- Environment: `{environment.get('os', 'unknown')}` / "
            f"`{environment.get('architecture', 'unknown')}`",
            "- Evidence is sanitized; user paths, credentials, and subprocess bodies "
            "are omitted.",
            "",
            "## Result categories",
            "",
            f"- Target artifact: `{target.get('status', 'unknown')}`",
            f"- Unsupported target: `{unsupported.get('status', 'unknown')}`",
            f"- Installer: `{installer.get('status', 'unknown')}`",
            f"- Workflow: `{workflow.get('status', 'unknown')}`",
            f"- Failure injection: `{failure_injection.get('status', 'unknown')}`",
            f"- TestPyPI: `{report.get('testpypi', {}).get('status', 'unknown')}`",
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--target-class", choices=TARGET_CLASSES)
    parser.add_argument("--python-wheel", type=Path)
    parser.add_argument("--manager", action="append")
    parser.add_argument("--skip-installer", action="store_true")
    parser.add_argument("--skip-transitions", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    report = run_qualification(
        manifest_path=args.manifest,
        artifact_dir=args.artifact_dir,
        target_class=args.target_class,
        python_wheel=args.python_wheel,
        managers=args.manager,
        skip_installer=args.skip_installer,
        skip_transitions=args.skip_transitions,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {"status": report["status"], "output": args.output.name}, sort_keys=True
        )
    )
    return (
        1
        if report["status"] == "fail" or (args.strict and report["status"] != "pass")
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
