#!/usr/bin/env python3
"""Build one version-pinned `eggpool-connect` helper executable.

The helper is built with plain Cargo (never Maturin): it is a native desktop
binary distributed as a GitHub release asset, not a Python wheel. Each target
class builds from the same clean release commit as the proxy wheel/raw pairs
so the release manifest can bind helper bytes to the same source commit.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from create_release_manifest import release_version
from inspect_connect_artifact import (
    CONNECT_TARGETS,
    ConnectInspectionError,
    connect_filename,
    inspect_connect_artifact,
)

ROOT = Path(__file__).resolve().parents[1]


class ConnectBuildError(ValueError):
    """A deterministic helper artifact build contract failure."""


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
        raise ConnectBuildError("source revision could not be checked") from error
    if result.stdout.strip():
        raise ConnectBuildError(
            "helper artifact builds require a clean source revision"
        )


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


def build_helper(target_class: str, output_dir: Path, cargo: str = "cargo") -> Path:
    """Build one helper executable and stage it under its release filename."""
    target = CONNECT_TARGETS.get(target_class)
    if target is None:
        raise ConnectBuildError(f"unsupported helper target class: {target_class}")
    _clean_source()
    version = release_version()
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise ConnectBuildError(
            "artifact directory must be empty before a helper build"
        )
    environment = os.environ.copy()
    _prefer_rustup_toolchain(environment)
    if target_class == "connect-macos-arm64":
        environment["MACOSX_DEPLOYMENT_TARGET"] = "11.0"
    command = [
        cargo,
        "build",
        "--manifest-path",
        str(ROOT / "rust/Cargo.toml"),
        "-p",
        "eggpool-connect",
        "--bin",
        "eggpool-connect",
        "--release",
        "--locked",
        "--target",
        str(target["rust_target"]),
    ]
    try:
        subprocess.run(command, cwd=ROOT, env=environment, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise ConnectBuildError(f"cargo build failed for {target_class}") from error
    binary_name = (
        "eggpool-connect.exe" if str(target["os"]) == "windows" else "eggpool-connect"
    )
    built = ROOT / "rust/target" / str(target["rust_target"]) / "release" / binary_name
    if not built.is_file():
        raise ConnectBuildError(
            f"helper build did not emit a binary for {target_class}"
        )
    staged = output_dir / connect_filename(version, target_class)
    staged.write_bytes(built.read_bytes())
    if str(target["os"]) != "windows":
        staged.chmod(0o755)
    try:
        inspect_connect_artifact(
            staged, expected_version=version, target_class=target_class
        )
    except ConnectInspectionError as error:
        raise ConnectBuildError(
            f"built helper failed inspection for {target_class}"
        ) from error
    return staged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target-class", required=True, choices=sorted(CONNECT_TARGETS)
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cargo", default="cargo")
    args = parser.parse_args(argv)
    try:
        staged = build_helper(args.target_class, args.out.resolve(), args.cargo)
    except (ConnectBuildError, OSError) as error:
        print(f"helper artifact build failed: {error}", file=sys.stderr)
        return 1
    print(f"helper artifact built {staged.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
