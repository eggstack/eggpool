#!/usr/bin/env python3
"""Validate a complete K003 artifact directory and release manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, cast

from inspect_cutover_raw import TARGETS, RawInspectionError, inspect_raw
from inspect_cutover_wheel import WheelInspectionError, inspect_wheel

SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")


class ValidationError(ValueError):
    """A deterministic K003 artifact-set failure."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{label} is missing")
    return value


def _check_no_unsupported_files(artifact_dir: Path, expected: set[str]) -> None:
    actual = {path.name for path in artifact_dir.rglob("*") if path.is_file()}
    unexpected = sorted(
        name
        for name in actual - expected
        if name.endswith((".whl", ".tar.gz")) or name.startswith("eggpool-")
    )
    if unexpected:
        raise ValidationError(f"unsupported or unmanifested artifact: {unexpected[0]}")


def _artifact_path(artifact_dir: Path, filename: str) -> Path:
    matches = sorted(path for path in artifact_dir.rglob(filename) if path.is_file())
    if len(matches) != 1:
        raise ValidationError(f"artifact file is missing or duplicated: {filename}")
    return matches[0]


def _linux_evidence(binary: Path) -> dict[str, Any]:
    readelf = subprocess.run(
        ["readelf", "-V", str(binary)], capture_output=True, text=True, check=False
    )
    if readelf.returncode:
        raise ValidationError("readelf could not inspect the Linux executable")
    minor_versions = [
        int(match.group(2))
        for match in re.finditer(r"GLIBC_(\d+)\.(\d+)", readelf.stdout)
        if int(match.group(1)) == 2
    ]
    if minor_versions and max(minor_versions) > 17:
        raise ValidationError(
            "Linux executable requires a glibc symbol newer than 2.17"
        )
    dynamic = subprocess.run(
        ["readelf", "-d", str(binary)], capture_output=True, text=True, check=False
    )
    if dynamic.returncode:
        raise ValidationError("readelf could not inspect Linux dynamic dependencies")
    allowed = {
        "libc.so.6",
        "libgcc_s.so.1",
        "libm.so.6",
        "libdl.so.2",
        "libpthread.so.0",
        "librt.so.1",
        "libresolv.so.2",
        "libutil.so.1",
        "ld-linux-x86-64.so.2",
        "ld-linux-aarch64.so.1",
    }
    dependencies = re.findall(r"Shared library: \[(.*?)\]", dynamic.stdout)
    if set(dependencies) - allowed:
        raise ValidationError("Linux executable has an unexpected dynamic dependency")
    return {
        "tool": "readelf",
        "glibc_floor": "2.17",
        "observed_glibc_symbol_max": f"2.{max(minor_versions)}"
        if minor_versions
        else None,
        "dynamic_dependencies": sorted(dependencies),
    }


def _macos_evidence(binary: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["otool", "-l", str(binary)], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise ValidationError("otool could not inspect the macOS executable")
    match = re.search(r"minos\s+(\d+\.\d+)", result.stdout)
    if match is None:
        raise ValidationError("macOS executable has no deployment target load command")
    major, minor = (int(part) for part in match.group(1).split(".", 1))
    if (major, minor) > (11, 0):
        raise ValidationError("macOS executable deployment target is newer than 11.0")
    return {"tool": "otool", "deployment_target": f"{major}.{minor}"}


def validate_manifest(
    manifest_path: Path, artifact_dir: Path, *, portability: bool = False
) -> dict[str, Any]:
    """Validate all manifest records, payload correlations, and target absence."""
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError("manifest could not be read") from error
    if not isinstance(value, dict):
        raise ValidationError("unsupported release manifest schema")
    manifest = cast("dict[str, Any]", value)
    if manifest.get("manifest_version") != "m11-release-manifest.v1":
        raise ValidationError("unsupported release manifest schema")
    version = _require(manifest.get("release_version"), "release_version")
    commit = _require(manifest.get("source_commit"), "source_commit")
    if not COMMIT_RE.fullmatch(commit):
        raise ValidationError("source_commit is not immutable")
    for field in ("cargo_lock_sha256", "packaging_manifest_sha256"):
        if not SHA256_RE.fullmatch(_require(manifest.get(field), field)):
            raise ValidationError(f"{field} is not SHA-256")
    raw_records_value = manifest.get("artifacts")
    if not isinstance(raw_records_value, list):
        raise ValidationError("manifest must contain exactly the supported target set")
    raw_records = cast("list[object]", raw_records_value)
    if len(raw_records) != len(TARGETS):
        raise ValidationError("manifest must contain exactly the supported target set")
    expected_files: set[str] = {manifest_path.name}
    seen: set[str] = set()
    for raw_record in raw_records:
        if not isinstance(raw_record, dict):
            raise ValidationError("artifact record must be an object")
        record = cast("dict[str, Any]", raw_record)
        target_class = _require(record.get("product_target"), "product_target")
        if target_class not in TARGETS or target_class in seen:
            raise ValidationError("manifest target set is invalid")
        seen.add(target_class)
        if record.get("rust_target") != TARGETS[target_class]["rust_target"]:
            raise ValidationError("Rust target disagrees with target matrix")
        wheel = cast("dict[str, Any]", record.get("wheel"))
        raw = cast("dict[str, Any]", record.get("raw"))
        wheel_path = _artifact_path(
            artifact_dir, _require(wheel.get("filename"), "wheel.filename")
        )
        raw_path = _artifact_path(
            artifact_dir, _require(raw.get("filename"), "raw.filename")
        )
        expected_files.update({wheel_path.name, raw_path.name})
        try:
            inspection = inspect_wheel(
                wheel_path, expected_version=version, target_class=target_class
            )
            raw_inspection = inspect_raw(
                raw_path, expected_version=version, target_class=target_class
            )
        except (WheelInspectionError, RawInspectionError) as error:
            raise ValidationError(
                f"artifact validation failed for {target_class}"
            ) from error
        if (
            wheel.get("sha256") != _sha256(wheel_path)
            or wheel.get("size") != wheel_path.stat().st_size
        ):
            raise ValidationError(f"wheel digest/size mismatch for {target_class}")
        if (
            raw.get("sha256") != raw_inspection.sha256
            or raw.get("size") != raw_inspection.size
        ):
            raise ValidationError(f"raw digest/size mismatch for {target_class}")
        with zipfile.ZipFile(wheel_path) as archive:
            payload = archive.read(inspection.executable_member)
        payload_hash = hashlib.sha256(payload).hexdigest()
        if (
            wheel.get("executable_sha256") != payload_hash
            or raw_inspection.sha256 != payload_hash
        ):
            raise ValidationError(
                f"wheel/raw executable hash mismatch for {target_class}"
            )
        if portability:
            evidence = (
                _linux_evidence(raw_path)
                if target_class.startswith("linux-")
                else _macos_evidence(raw_path)
            )
            record["portability"] = evidence
    if seen != set(TARGETS):
        raise ValidationError("manifest target set is incomplete")
    _check_no_unsupported_files(artifact_dir, expected_files)
    if manifest.get("qualification_result") not in {"pending", "pass"}:
        raise ValidationError("manifest qualification result is invalid")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--portability", action="store_true")
    args = parser.parse_args(argv)
    try:
        value = validate_manifest(
            args.manifest.resolve(),
            args.artifact_dir.resolve(),
            portability=args.portability,
        )
    except (ValidationError, OSError, subprocess.SubprocessError) as error:
        print(f"K003 artifact set invalid: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {"status": "pass", "targets": len(value["artifacts"])}, sort_keys=True
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
