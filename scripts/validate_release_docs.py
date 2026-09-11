#!/usr/bin/env python3
"""Validate the release docs public metadata, documentation, and release guard."""

from __future__ import annotations

import argparse
import json
import re
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
PACKAGING = ROOT / "packaging/pypi/pyproject.toml"
ROOT_PYPROJECT = ROOT / "pyproject.toml"
WORKFLOW = ROOT / ".github/workflows/release.yml"
MANIFEST = ROOT / "packaging/release/release-manifest.json"
PUBLIC_DOCS = (
    ROOT / "README.md",
    ROOT / "docs/deployment.md",
    ROOT / "docs/upgrading.md",
    ROOT / "docs/raspberry-pi.md",
    ROOT / "docs/rust-release-deployment.md",
    ROOT / "docs/releasing.md",
    ROOT / "packaging/pypi/README.md",
)
LOCAL_LINK_RE = re.compile(r"!?\[[^\]]+\]\(([^)#]+)(?:#[^)]+)?\)")
VERSION_RE = re.compile(r"\b(?:v)?\d+\.\d+\.\d+\b")


class ReleaseDocsError(ValueError):
    """A release docs public metadata or documentation contract failure."""


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReleaseDocsError(f"{path} is not a JSON object")
    return cast("dict[str, Any]", value)


def _require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise ReleaseDocsError(f"{label} is missing: {needle}")


def _check_local_links(path: Path, text: str) -> None:
    for target in LOCAL_LINK_RE.findall(text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        resolved = (path.parent / target).resolve()
        if not resolved.exists():
            raise ReleaseDocsError(f"broken local link in {path.name}: {target}")


def validate_release_docs() -> dict[str, object]:
    try:
        validate_package_boundary()
    except PackageBoundaryError as error:
        raise ReleaseDocsError(
            f"current package boundary is invalid: {error}"
        ) from error
    catalog = _read_json(CATALOG)
    authority = cast("dict[str, Any]", catalog["version_authority"])
    version = str(authority["native_release_version"])
    targets = cast("dict[str, Any]", catalog["target_matrix"])
    supported = {
        name
        for name, value in targets.items()
        if cast("dict[str, Any]", value).get("classification")
        in {"supported", "supported-development"}
    }
    expected_targets = {"linux-x86_64", "linux-aarch64", "macos-arm64"}
    if supported != expected_targets:
        raise ReleaseDocsError("release catalog supported target set changed")

    cargo = _read_toml(ROOT / "rust/Cargo.toml")["package"]
    publication = _read_toml(PACKAGING)
    project = cast("dict[str, Any]", publication["project"])
    root_tooling = cast(
        "dict[str, Any]",
        cast("dict[str, Any]", _read_toml(ROOT_PYPROJECT).get("tool", {})).get(
            "eggpool", {}
        ),
    )
    if cargo["version"] != version:
        raise ReleaseDocsError("Cargo version disagrees with release catalog")
    if project.get("dynamic") != ["version"] or project.get("name") != "eggpool":
        raise ReleaseDocsError("Rust publication manifest is not Maturin-owned")
    if project.get("requires-python") != ">=3.11":
        raise ReleaseDocsError("Rust wheel Requires-Python floor changed")
    if root_tooling.get("historical_python_version") == version:
        raise ReleaseDocsError("tooling marker uses the Rust native release version")
    classifiers = {str(item) for item in project.get("classifiers", [])}
    if {"Framework :: FastAPI", "Framework :: AsyncIO"} & classifiers:
        raise ReleaseDocsError("Rust wheel retains Python-runtime classifiers")
    if "Programming Language :: Rust" not in classifiers:
        raise ReleaseDocsError("Rust wheel classifier is missing")

    manifest = _read_json(MANIFEST)
    if manifest.get("release_version") != version:
        raise ReleaseDocsError(
            "staged release manifest version disagrees with release catalog"
        )
    artifacts = manifest.get("artifacts")
    artifact_targets: set[str] = set()
    if isinstance(artifacts, list):
        for raw_item in cast("list[object]", artifacts):
            if isinstance(raw_item, dict):
                item = cast("dict[str, Any]", raw_item)
                artifact_targets.add(str(item.get("product_target")))
    if artifact_targets != expected_targets:
        raise ReleaseDocsError(
            "staged artifact target set disagrees with release catalog"
        )

    contents = {path: path.read_text(encoding="utf-8") for path in PUBLIC_DOCS}
    for path, text in contents.items():
        _check_local_links(path, text)
    readme = contents[ROOT / "README.md"]
    deployment = contents[ROOT / "docs/deployment.md"]
    upgrading = contents[ROOT / "docs/upgrading.md"]
    releasing = contents[ROOT / "docs/releasing.md"]
    installer = (ROOT / "scripts/install.sh").read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    publication_status = authority.get("publication_status")

    target_labels = {
        "linux-x86_64": ("linux x86_64", "linux-x86_64"),
        "linux-aarch64": ("linux aarch64", "linux-aarch64"),
        "macos-arm64": ("macOS arm64", "macos-arm64"),
    }
    for label, text in (
        ("README", readme),
        ("deployment", deployment),
        ("upgrading", upgrading),
    ):
        lowered = text.lower()
        for target in expected_targets:
            found = any(
                spelling.lower() in lowered for spelling in target_labels[target]
            )
            if target == "macos-arm64":
                found = "macos" in lowered and "arm64" in lowered
            if not found:
                raise ReleaseDocsError(
                    f"{label} target documentation is missing: {target}"
                )
        _require(text, "Windows", f"{label} unsupported-target documentation")
        _require(text.lower(), "unsupported", f"{label} unsupported-target status")
    _require(readme, "scripts/install.sh", "README canonical installer")
    _require(readme, "uv tool install eggpool", "README uv package install")
    _require(readme, "pipx install eggpool", "README pipx package install")
    if "git clone" in readme or "uv sync --no-dev" in readme:
        raise ReleaseDocsError("README quick start routes normal users to source")
    for command in ("eggpool update", "eggpool update 0.8.0", "install-provenance"):
        _require(readme, command, "README update/provenance guidance")

    for phrase in (
        "0.6.7 through 0.7.4",
        "never resets",
        "verified GitHub raw",
        "ambiguous ownership",
        "source-build fallback is allowed",
    ):
        _require(upgrading, phrase, "upgrade/rollback guide")
    _require(releasing, "validate_release_docs.py", "release guard documentation")
    _require(changelog, f"## [{version}]", "changelog release heading")
    if (
        publication_status == "published"
        and "Release candidate"
        in changelog.split(f"## [{version}]", 1)[1].split("## [", 1)[0]
    ):
        raise ReleaseDocsError("published release retains candidate changelog wording")
    for phrase in ("immutable external artifacts", "runtime", "unsupported"):
        _require(changelog, phrase, "changelog Python-reference/supported-target note")

    stale_patterns = ("Granian", "FastAPI", "public Python installation")
    for path, text in contents.items():
        if any(pattern in text for pattern in stale_patterns):
            raise ReleaseDocsError(f"stale Python runtime language in {path}")
    if "git clone" in deployment or "uv sync --no-dev" in deployment:
        raise ReleaseDocsError("deployment normal path still requires a checkout")
    if re.search(r"(?im)^\s*source\s+[^#\n]*\.bashrc", deployment):
        raise ReleaseDocsError("deployment sources a shell rc file")

    _require(installer, 'PACKAGE_SPEC="eggpool"', "installer PyPI default")
    _require(
        installer,
        "EGGPOOL_INSTALL_ALLOW_NONPRODUCTION_INDEX",
        "staged installer guard",
    )
    if "git clone" in installer:
        raise ReleaseDocsError("installer contains a hidden repository fallback")

    _require(workflow, "build_release_artifacts.py", "Rust artifact workflow")
    if re.search(r"(?im)\buv\s+(build|publish)\b", workflow):
        raise ReleaseDocsError(
            "production workflow can select the root Hatchling artifact"
        )
    if "py3-none-any" in workflow or "windows" in workflow.lower():
        raise ReleaseDocsError("release workflow contains an unsupported artifact path")

    update_examples = VERSION_RE.findall(readme + deployment + upgrading)
    if not update_examples or any(
        value.removeprefix("v").count(".") != 2 for value in update_examples
    ):
        raise ReleaseDocsError("update examples are not normalized X.Y.Z targets")

    return {
        "status": "pass",
        "version": version,
        "targets": sorted(expected_targets),
        "docs_checked": len(PUBLIC_DOCS),
        "production_release": (
            f"published {version}"
            if publication_status == "published"
            else "guarded until publication"
        ),
        "python_reference": "historical external artifacts",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        print(json.dumps(validate_release_docs(), sort_keys=True))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(
            f"release documentation validation failed: {error}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
