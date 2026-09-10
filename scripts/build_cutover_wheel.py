#!/usr/bin/env python3
"""Build and validate one K002 Rust binary wheel."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, cast

from inspect_cutover_wheel import WheelInspectionError, inspect_wheel

ROOT = Path(__file__).resolve().parents[1]
PACKAGING_DIR = ROOT / "packaging/pypi"
PUBLICATION_MANIFEST = PACKAGING_DIR / "pyproject.toml"
CARGO_MANIFEST = ROOT / "rust/Cargo.toml"
CATALOG = ROOT / "migration-rs/fixtures/cutover/k001-installable-releases.json"
SUPPORTED_TARGETS = {
    "x86_64-unknown-linux-gnu": "linux-x86_64",
    "aarch64-unknown-linux-gnu": "linux-aarch64",
    "aarch64-apple-darwin": "macos-arm64",
}


class BuildContractError(ValueError):
    """A pre-build or post-build K002 contract failure."""


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise BuildContractError(
            f"cannot read packaging metadata: {path.name}"
        ) from error
    return value


def _table(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BuildContractError(f"{name} must be a TOML table")
    return cast("dict[str, Any]", value)


def _preflight() -> str:
    publication = _read_toml(PUBLICATION_MANIFEST)
    project = _table(publication.get("project"), "project")
    build_system = _table(publication.get("build-system"), "build-system")
    maturin = _table(
        _table(publication.get("tool"), "tool").get("maturin"), "tool.maturin"
    )
    cargo = _table(_read_toml(CARGO_MANIFEST).get("package"), "package")
    dynamic = project.get("dynamic")
    if (
        project.get("name") != "eggpool"
        or not isinstance(dynamic, list)
        or "version" not in dynamic
    ):
        raise BuildContractError(
            "publication project must use dynamic Cargo version metadata"
        )
    if project.get("requires-python") != ">=3.11":
        raise BuildContractError("publication Requires-Python must be >=3.11")
    requires = build_system.get("requires")
    if (
        requires != ["maturin==1.14.1"]
        or build_system.get("build-backend") != "maturin"
    ):
        raise BuildContractError(
            "publication build backend is not the reviewed Maturin pin"
        )
    if (
        maturin.get("bindings") != "bin"
        or maturin.get("manifest-path") != "../../rust/Cargo.toml"
    ):
        raise BuildContractError(
            "publication manifest does not select the Rust binary crate"
        )
    if maturin.get("locked") is not True or maturin.get("compatibility") != "pypi":
        raise BuildContractError(
            "publication manifest does not enforce locked PyPI-compatible builds"
        )
    if maturin.get("strip") is not False:
        raise BuildContractError(
            "publication manifest must keep diagnostics by disabling stripping"
        )
    if any(
        key in maturin for key in ("python-source", "module-name", "sdist-generator")
    ):
        raise BuildContractError(
            "publication manifest contains extension or sdist configuration"
        )
    if cargo.get("version") != _candidate_version():
        raise BuildContractError(
            "Cargo version does not equal the K001 cutover candidate"
        )
    if not CARGO_MANIFEST.with_name("Cargo.lock").is_file():
        raise BuildContractError("Cargo.lock is required for a cutover build")
    return str(cargo["version"])


def _candidate_version() -> str:
    catalog = cast("dict[str, Any]", json.loads(CATALOG.read_text(encoding="utf-8")))
    authority = _table(catalog.get("version_authority"), "version_authority")
    version = authority.get("cutover_version")
    if not isinstance(version, str):
        raise BuildContractError("K001 cutover version is missing")
    return version


def _host_target() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux" and machine in {"x86_64", "amd64"}:
        return "x86_64-unknown-linux-gnu"
    if system == "linux" and machine in {"aarch64", "arm64"}:
        return "aarch64-unknown-linux-gnu"
    if system == "darwin" and machine in {"arm64", "aarch64"}:
        return "aarch64-apple-darwin"
    raise BuildContractError(
        "host is not a qualified K002 build target; pass an explicitly "
        "qualified --target"
    )


def _prefer_rustup_toolchain(environment: dict[str, str]) -> None:
    """Use the active rustup toolchain when both rustup and system Cargo exist."""

    rustup = shutil.which("rustup")
    if rustup is None:
        return
    try:
        cargo = subprocess.run(
            [rustup, "which", "cargo"], check=True, capture_output=True, text=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return
    cargo_dir = str(Path(cargo).parent) if cargo else ""
    if cargo_dir:
        environment["PATH"] = os.pathsep.join([cargo_dir, environment.get("PATH", "")])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=None)
    parser.add_argument("--out", type=Path, default=ROOT / "dist/cutover")
    parser.add_argument("--release-version")
    parser.add_argument("--release-tag")
    parser.add_argument("--maturin", default="maturin")
    args = parser.parse_args(argv)
    try:
        version = _preflight()
        target = args.target or _host_target()
        target_class = SUPPORTED_TARGETS.get(target)
        if target_class is None:
            raise BuildContractError(f"unsupported Rust build target: {target}")
        if args.release_version and args.release_version != version:
            raise BuildContractError(
                "release version does not equal Cargo/K001 candidate"
            )
        if args.release_tag and args.release_tag.removeprefix("v") != version:
            raise BuildContractError("release tag does not equal Cargo/K001 candidate")
        output_dir = args.out if args.out.is_absolute() else ROOT / args.out
        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        if any(output_dir.glob("*.whl")) or any(output_dir.glob("*.tar.gz")):
            raise BuildContractError(
                "output directory contains an existing wheel or sdist"
            )
        maturin_version = subprocess.run(
            [args.maturin, "--version"], check=True, capture_output=True, text=True
        ).stdout.strip()
        if maturin_version != "maturin 1.14.1":
            raise BuildContractError(
                "Maturin executable does not match the reviewed 1.14.1 pin"
            )
        environment = os.environ.copy()
        _prefer_rustup_toolchain(environment)
        if target == "aarch64-apple-darwin":
            environment["MACOSX_DEPLOYMENT_TARGET"] = "11.0"
        command = [
            args.maturin,
            "build",
            "--locked",
            "--release",
            "--target",
            target,
            "--compatibility",
            "pypi",
            "--strip",
            "false",
            "--out",
            str(output_dir),
        ]
        subprocess.run(command, cwd=PACKAGING_DIR, env=environment, check=True)
        wheels = list(output_dir.glob("*.whl"))
        sdists = list(output_dir.glob("*.tar.gz"))
        if len(wheels) != 1:
            raise BuildContractError("build did not emit exactly one wheel")
        if sdists:
            raise BuildContractError("Rust cutover build emitted an sdist")
        result = inspect_wheel(
            wheels[0], expected_version=version, target_class=target_class
        )
        print(
            json.dumps(
                {
                    "maturin": maturin_version,
                    "target": target,
                    "inspection": result.__dict__,
                },
                default=list,
                sort_keys=True,
            )
        )
        return 0
    except (
        BuildContractError,
        WheelInspectionError,
        OSError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"K002 build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
