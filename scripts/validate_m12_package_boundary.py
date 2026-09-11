#!/usr/bin/env python3
"""Validate the M12 current-package and historical-tooling boundary."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "migration-rs/fixtures/cutover/k001-installable-releases.json"
PUBLICATION = ROOT / "packaging/pypi/pyproject.toml"
ROOT_PROJECT = ROOT / "pyproject.toml"
WORKFLOW = ROOT / ".github/workflows/release.yml"
VERSION_RE = re.compile(r"\A[0-9]+\.[0-9]+\.[0-9]+\Z")


class PackageBoundaryError(ValueError):
    """The current Rust publication boundary is unsafe or ambiguous."""


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise PackageBoundaryError(f"cannot read TOML boundary file: {path}") from error
    return value


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PackageBoundaryError(f"cannot read JSON boundary file: {path}") from error
    if not isinstance(value, dict):
        raise PackageBoundaryError(f"boundary file is not an object: {path}")
    return cast("dict[str, Any]", value)


def _version(value: object, label: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or VERSION_RE.fullmatch(value) is None:
        raise PackageBoundaryError(f"{label} is not a stable X.Y.Z version")
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def validate_package_boundary(
    *, root: Path = ROOT, workflow_text: str | None = None
) -> dict[str, str]:
    """Validate that only the packaging/pypi manifest is current authority."""

    root_project = _read_toml(root / "pyproject.toml")
    root_metadata = cast("dict[str, Any]", root_project.get("project", {}))
    retirement = cast(
        "dict[str, Any]",
        cast("dict[str, Any]", root_project.get("tool", {})).get("eggpool", {}),
    )
    if retirement.get("project_role") != "historical-development-only":
        raise PackageBoundaryError(
            "root pyproject.toml must declare historical-development-only role"
        )
    if retirement.get("current_runtime") != "rust":
        raise PackageBoundaryError("root project must identify Rust as current runtime")
    if retirement.get("publication_manifest") != "packaging/pypi/pyproject.toml":
        raise PackageBoundaryError(
            "root project must point at the sole publication manifest"
        )

    catalog = _read_json(root / CATALOG.relative_to(ROOT))
    authority = cast("dict[str, Any]", catalog.get("version_authority", {}))
    cutover = _version(authority.get("cutover_version"), "catalog cutover")
    historical = _version(
        authority.get("historical_python_project_version"),
        "historical Python version",
    )
    root_version = _version(root_metadata.get("version"), "root project version")
    if root_version != historical:
        raise PackageBoundaryError(
            "root project is not pinned to the historical Python version"
        )
    if root_version >= cutover:
        raise PackageBoundaryError(
            "root historical project is at or beyond Rust cutover"
        )
    if root_metadata.get("name") != "eggpool":
        raise PackageBoundaryError(
            "historical root project must retain EggPool identity"
        )

    publication = _read_toml(root / PUBLICATION.relative_to(ROOT))
    project = cast("dict[str, Any]", publication.get("project", {}))
    if project.get("name") != "eggpool" or project.get("dynamic") != ["version"]:
        raise PackageBoundaryError("current publication is not Cargo-versioned EggPool")
    build_system = cast("dict[str, Any]", publication.get("build-system", {}))
    if build_system.get("build-backend") != "maturin":
        raise PackageBoundaryError("current publication does not use Maturin")
    maturin = cast(
        "dict[str, Any]",
        cast("dict[str, Any]", publication.get("tool", {})).get("maturin", {}),
    )
    if maturin.get("bindings") != "bin" or maturin.get("locked") is not True:
        raise PackageBoundaryError("current publication is not a locked native binary")
    if maturin.get("manifest-path") != "../../rust/Cargo.toml":
        raise PackageBoundaryError("current publication does not name Cargo authority")

    workflow = (
        (root / WORKFLOW.relative_to(ROOT)).read_text(encoding="utf-8")
        if workflow_text is None
        else workflow_text
    )
    forbidden = (
        r"\buv\s+(?:build|publish)\b",
        r"\b(?:python|python3)\s+-m\s+(?:build|twine)\b",
        r"\bhatch\s+build\b",
        r"src/eggpool",
        r"pyproject\.toml\s+--(?:out|output)",
    )
    for pattern in forbidden:
        if re.search(pattern, workflow, re.IGNORECASE):
            raise PackageBoundaryError(
                "release workflow contains a root/historical Python publication path"
            )
    if "packaging/pypi/pyproject.toml" not in workflow:
        raise PackageBoundaryError(
            "release workflow does not name the current manifest"
        )
    return {
        "status": "pass",
        "current_runtime": "rust",
        "publication_manifest": "packaging/pypi/pyproject.toml",
        "historical_python_version": str(
            authority["historical_python_project_version"]
        ),
        "cutover_version": str(authority["cutover_version"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", type=Path, default=WORKFLOW)
    args = parser.parse_args(argv)
    try:
        workflow = args.workflow.resolve().read_text(encoding="utf-8")
        print(
            json.dumps(
                validate_package_boundary(workflow_text=workflow), sort_keys=True
            )
        )
    except (OSError, PackageBoundaryError) as error:
        print(f"M12 package boundary validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
