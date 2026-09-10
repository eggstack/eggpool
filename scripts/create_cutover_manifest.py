#!/usr/bin/env python3
"""Create a deterministic K003 manifest from a complete artifact directory."""

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
from inspect_cutover_wheel import TARGET_PLATFORMS, WheelInspectionError, inspect_wheel

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "migration-rs/fixtures/cutover/k001-installable-releases.json"
PACKAGING_MANIFEST = ROOT / "packaging/pypi/pyproject.toml"
CARGO_LOCK = ROOT / "rust/Cargo.lock"
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")


class ManifestError(ValueError):
    """A deterministic, secret-free release manifest failure."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def candidate_version() -> str:
    with CATALOG.open(encoding="utf-8") as handle:
        catalog = cast("dict[str, Any]", json.load(handle))
    authority = cast("dict[str, Any]", catalog["version_authority"])
    value = authority.get("cutover_version")
    if not isinstance(value, str):
        raise ManifestError("catalog cutover version is missing")
    return value


def _source_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ManifestError("source commit could not be resolved") from error
    commit = result.stdout.strip()
    if not COMMIT_RE.fullmatch(commit):
        raise ManifestError("source commit is not an immutable SHA")
    return commit


def _tool_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for command, label in (
        (["maturin", "--version"], "maturin"),
        (["rustc", "-V"], "rust"),
    ):
        try:
            result = subprocess.run(
                command, check=True, capture_output=True, text=True, timeout=10
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            versions[label] = "unavailable"
        else:
            versions[label] = " ".join(result.stdout.split())[:128]
    return versions


def _raw_path(artifact_dir: Path, version: str, target_class: str) -> Path:
    target = TARGETS[target_class]
    name = f"eggpool-{version}-{target['os']}-{target['arch']}"
    matches = sorted(path for path in artifact_dir.rglob(name) if path.is_file())
    if len(matches) != 1:
        raise ManifestError(f"expected exactly one raw asset for {target_class}")
    return matches[0]


def _wheel_record(
    artifact_dir: Path,
    version: str,
    target_class: str,
    qualification_result: str,
) -> dict[str, Any]:
    wheels = [
        path
        for path in sorted(artifact_dir.rglob(f"eggpool-{version}-*.whl"))
        if any(
            path.name.endswith(f"{platform_tag}.whl")
            for platform_tag in TARGET_PLATFORMS[target_class]
        )
    ]
    if len(wheels) != 1:
        raise ManifestError(f"expected exactly one wheel for {target_class}")
    wheel = wheels[0]
    try:
        inspection = inspect_wheel(
            wheel, expected_version=version, target_class=target_class
        )
    except WheelInspectionError as error:
        raise ManifestError(f"wheel validation failed for {target_class}") from error
    with zipfile.ZipFile(wheel) as archive:
        executable = archive.read(inspection.executable_member)
    raw = _raw_path(artifact_dir, version, target_class)
    try:
        raw_inspection = inspect_raw(
            raw, expected_version=version, target_class=target_class
        )
    except RawInspectionError as error:
        raise ManifestError(f"raw validation failed for {target_class}") from error
    executable_hash = hashlib.sha256(executable).hexdigest()
    if (
        executable_hash != raw_inspection.sha256
        or len(executable) != raw_inspection.size
    ):
        raise ManifestError(f"wheel/raw payload differs for {target_class}")
    return {
        "product_target": target_class,
        "rust_target": TARGETS[target_class]["rust_target"],
        "wheel": {
            "filename": wheel.name,
            "platform_tags": list(inspection.tags),
            "sha256": _sha256(wheel),
            "size": wheel.stat().st_size,
            "executable_member": inspection.executable_member,
            "executable_sha256": executable_hash,
            "executable_size": len(executable),
            "requires_python": inspection.requires_python,
        },
        "raw": {
            "filename": raw.name,
            "sha256": raw_inspection.sha256,
            "size": raw_inspection.size,
        },
        "build": {
            "tool_versions": _tool_versions(),
            "linux_compatibility": (
                "manylinux2014 / glibc 2.17 floor"
                if target_class.startswith("linux-")
                else None
            ),
            "macos_deployment_target": "11.0"
            if target_class == "macos-arm64"
            else None,
        },
        "qualification": {"result": qualification_result},
    }


def create_manifest(
    artifact_dir: Path, output: Path, *, qualification_result: str = "pending"
) -> dict[str, Any]:
    """Create and write the bounded manifest for all three supported targets."""
    version = candidate_version()
    if qualification_result not in {"pending", "pass"}:
        raise ManifestError("qualification result must be pending or pass")
    records = [
        _wheel_record(artifact_dir, version, target, qualification_result)
        for target in sorted(TARGETS)
    ]
    manifest: dict[str, Any] = {
        "manifest_version": "m11-release-manifest.v1",
        "release_version": version,
        "source_commit": _source_commit(),
        "cargo_lock_sha256": _sha256(CARGO_LOCK),
        "packaging_manifest_sha256": _sha256(PACKAGING_MANIFEST),
        "requires_python": ">=3.11",
        "artifacts": records,
        "historical_backfill_candidates": [],
        "qualification_result": qualification_result,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--qualification-result", choices=("pending", "pass"), default="pending"
    )
    args = parser.parse_args(argv)
    try:
        manifest = create_manifest(
            args.artifact_dir.resolve(),
            args.output.resolve(),
            qualification_result=args.qualification_result,
        )
    except (ManifestError, OSError, json.JSONDecodeError) as error:
        print(f"K003 manifest creation failed: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "manifest": args.output.name,
                "sha256": _sha256(args.output),
                "targets": len(manifest["artifacts"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
