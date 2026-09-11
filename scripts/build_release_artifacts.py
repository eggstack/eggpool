#!/usr/bin/env python3
"""Build a release artifact wheel/raw pair with pinned target settings."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from create_release_manifest import release_version
from inspect_release_raw import TARGETS, raw_filename
from inspect_release_wheel import WheelInspectionError, inspect_wheel
from validate_runtime_package_boundary import (
    PackageBoundaryError,
    validate_package_boundary,
)

ROOT = Path(__file__).resolve().parents[1]
PACKAGING_DIR = ROOT / "packaging/pypi"
MATURIN_VERSION = "maturin 1.14.1"


class BuildError(ValueError):
    """A deterministic release artifacts build contract failure."""


def _clean_source() -> None:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise BuildError("source revision could not be checked") from error
    if result.stdout.strip():
        raise BuildError("release artifact builds require a clean source revision")


def _prefer_rustup_toolchain(environment: dict[str, str]) -> None:
    rustup = shutil.which("rustup")
    if rustup is None:
        return
    try:
        cargo = subprocess.run(
            [rustup, "which", "cargo"], check=True, capture_output=True, text=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return
    if cargo:
        environment["PATH"] = os.pathsep.join(
            [str(Path(cargo).parent), environment.get("PATH", "")]
        )


def build_pair(
    target_class: str, output_dir: Path, maturin: str = "maturin"
) -> tuple[Path, Path]:
    """Build one wheel and mechanically derive its raw executable."""
    try:
        validate_package_boundary()
    except PackageBoundaryError as error:
        raise BuildError(f"current package boundary is invalid: {error}") from error
    target = TARGETS.get(target_class)
    if target is None:
        raise BuildError(f"unsupported target class: {target_class}")
    _clean_source()
    version = release_version()
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise BuildError("artifact directory must be empty before a target build")
    try:
        version_output = subprocess.run(
            [maturin, "--version"], check=True, capture_output=True, text=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise BuildError("Maturin could not be executed") from error
    if version_output != MATURIN_VERSION:
        raise BuildError("Maturin executable does not match the reviewed 1.14.1 pin")
    environment = os.environ.copy()
    _prefer_rustup_toolchain(environment)
    if target_class == "macos-arm64":
        environment["MACOSX_DEPLOYMENT_TARGET"] = "11.0"
    command = [
        maturin,
        "build",
        "--locked",
        "--release",
        "--target",
        str(target["rust_target"]),
        "--compatibility",
        "manylinux2014" if target_class.startswith("linux-") else "pypi",
        "--strip",
        "false",
        "--out",
        str(output_dir),
    ]
    if target_class.startswith("linux-"):
        command.extend(["--auditwheel", "check", "--zig"])
    try:
        subprocess.run(command, cwd=PACKAGING_DIR, env=environment, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise BuildError(f"Maturin build failed for {target_class}") from error
    wheels = sorted(output_dir.glob("*.whl"))
    if len(wheels) != 1 or any(output_dir.glob("*.tar.gz")):
        raise BuildError("target build did not emit exactly one wheel and no sdist")
    wheel = wheels[0]
    try:
        inspection = inspect_wheel(
            wheel, expected_version=version, target_class=target_class
        )
    except WheelInspectionError as error:
        raise BuildError(f"built wheel failed inspection for {target_class}") from error
    raw = output_dir / raw_filename(version, target_class)
    with zipfile.ZipFile(wheel) as archive:
        raw.write_bytes(archive.read(inspection.executable_member))
    raw.chmod(0o755)
    return wheel, raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-class", required=True, choices=sorted(TARGETS))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--maturin", default="maturin")
    args = parser.parse_args(argv)
    try:
        wheel, raw = build_pair(args.target_class, args.out.resolve(), args.maturin)
    except (BuildError, OSError) as error:
        print(f"release artifacts build failed: {error}", file=sys.stderr)
        return 1
    print(f"release artifacts built {wheel.name} and {raw.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
