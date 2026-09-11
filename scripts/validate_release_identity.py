#!/usr/bin/env python3
"""Validate release catalog and packaging identity before a workflow build."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, cast

from validate_runtime_package_boundary import (
    PackageBoundaryError,
    validate_package_boundary,
)

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "rust/assets/catalog/installable-releases.json"
CARGO = ROOT / "rust/Cargo.toml"
PACKAGING = ROOT / "packaging/pypi/pyproject.toml"
VERSION_RE = re.compile(r"\A[0-9]+\.[0-9]+\.[0-9]+\Z")
COMMIT_RE = re.compile(r"\A[0-9a-f]{40}\Z")


class ReleaseValidationError(ValueError):
    """The tag/source/package identity is not safe to release."""


def _read(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        value = tomllib.load(handle) if path.suffix == ".toml" else json.load(handle)
    if not isinstance(value, dict):
        raise ReleaseValidationError(f"{path} is not a mapping")
    return cast("dict[str, Any]", value)


def _git(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ReleaseValidationError("git identity could not be resolved") from error
    return result.stdout.strip()


def validate_release_identity(
    *, tag: str | None = None, source_commit: str | None = None
) -> dict[str, str]:
    try:
        validate_package_boundary()
    except PackageBoundaryError as error:
        raise ReleaseValidationError(
            f"current package boundary is invalid: {error}"
        ) from error
    catalog = _read(CATALOG)
    authority = cast("dict[str, Any]", catalog.get("version_authority"))
    version = authority.get("native_release_version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ReleaseValidationError("catalog native release version is invalid")

    cargo_package = cast("dict[str, Any]", _read(CARGO).get("package"))
    if cargo_package.get("version") != version:
        raise ReleaseValidationError(
            "Cargo version disagrees with the release catalog native release version"
        )
    if cargo_package.get("publish") is not False:
        raise ReleaseValidationError(
            "the application Cargo package must not be published directly"
        )

    packaging = _read(PACKAGING)
    project = cast("dict[str, Any]", packaging.get("project"))
    if project.get("name") != "eggpool" or project.get("dynamic") != ["version"]:
        raise ReleaseValidationError(
            "publication metadata does not use Cargo version authority"
        )
    if project.get("requires-python") != ">=3.11":
        raise ReleaseValidationError("publication Requires-Python floor changed")
    build_system = cast("dict[str, Any]", packaging.get("build-system"))
    if (
        build_system.get("requires") != ["maturin==1.14.1"]
        or build_system.get("build-backend") != "maturin"
    ):
        raise ReleaseValidationError(
            "Maturin build pin/backend is not the reviewed release packaging contract"
        )
    maturin = cast(
        "dict[str, Any]", cast("dict[str, Any]", packaging.get("tool")).get("maturin")
    )
    if (
        maturin.get("bindings") != "bin"
        or maturin.get("manifest-path") != "../../rust/Cargo.toml"
    ):
        raise ReleaseValidationError("publication manifest is not a Rust binary wheel")
    if maturin.get("locked") is not True or maturin.get("compatibility") != "pypi":
        raise ReleaseValidationError(
            "publication manifest is not lockfile/PyPI constrained"
        )

    if tag is not None:
        match = re.fullmatch(r"v([0-9]+\.[0-9]+\.[0-9]+)", tag)
        if match is None or match.group(1) != version:
            raise ReleaseValidationError(
                "release tag does not match the catalogued version"
            )
        tag_commit = _git("rev-parse", f"{tag}^{{commit}}")
        if source_commit is not None and tag_commit != source_commit:
            raise ReleaseValidationError(
                "release tag does not point at the workflow source commit"
            )

    head = _git("rev-parse", "HEAD")
    if not COMMIT_RE.fullmatch(head):
        raise ReleaseValidationError("checked-out source is not an immutable commit")
    if source_commit is not None and (
        not COMMIT_RE.fullmatch(source_commit) or head != source_commit
    ):
        raise ReleaseValidationError(
            "workflow source commit does not match checked-out source"
        )
    return {"status": "pass", "version": version, "source_commit": head}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag")
    parser.add_argument("--source-commit")
    args = parser.parse_args(argv)
    try:
        print(
            json.dumps(
                validate_release_identity(
                    tag=args.tag, source_commit=args.source_commit
                ),
                sort_keys=True,
            )
        )
    except (
        OSError,
        ReleaseValidationError,
        json.JSONDecodeError,
        tomllib.TOMLDecodeError,
    ) as error:
        print(
            f"release identity validation failed: {error}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
