#!/usr/bin/env python3
"""Validate the P003 Rust-owned asset and retired-source boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "rust/assets/runtime-manifest.json"
LEGACY_SOURCE = ROOT / "src/eggpool"

CURRENT_PATHS = (
    ROOT / "rust/src",
    ROOT / "rust/build.rs",
    ROOT / "rust/build_support.rs",
    ROOT / "packaging",
    ROOT / ".github/workflows/release.yml",
    ROOT / "scripts/build_cutover_artifacts.py",
    ROOT / "scripts/check_cutover_catalog.py",
    ROOT / "scripts/create_cutover_manifest.py",
    ROOT / "scripts/inspect_cutover_wheel.py",
    ROOT / "scripts/qualify_cutover_wheel.py",
    ROOT / "scripts/validate_cutover_artifacts.py",
    ROOT / "scripts/validate_cutover_docs.py",
    ROOT / "scripts/validate_cutover_release.py",
    ROOT / "scripts/validate_release_workflow.py",
)

FORBIDDEN_CURRENT_REFERENCES = (
    re.compile(r"src/eggpool"),
    re.compile(r"python\s+-m\s+eggpool", re.IGNORECASE),
    re.compile(r"\b(?:from|import)\s+eggpool\b"),
    re.compile(r"PYTHONPATH\s*=\s*[^\n]*?(?:src/eggpool|/src(?:[/'\"]|$))"),
    re.compile(r"eggpool\.cli\s*:\s*main"),
    re.compile(r"packages\s*=\s*\[\s*[\"']src/eggpool[\"']"),
)


class RetirementBoundaryError(ValueError):
    """The current Rust source or asset boundary is unsafe."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RetirementBoundaryError(
            f"cannot read JSON boundary file: {path}"
        ) from error
    if not isinstance(value, dict):
        raise RetirementBoundaryError(f"boundary file is not an object: {path}")
    return cast("dict[str, Any]", value)


def _iter_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(item for item in path.rglob("*") if item.is_file())


def _validate_references() -> None:
    if LEGACY_SOURCE.exists():
        raise RetirementBoundaryError(
            "retired src/eggpool application tree still exists"
        )

    for root in CURRENT_PATHS:
        for path in _iter_files(root):
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for pattern in FORBIDDEN_CURRENT_REFERENCES:
                if pattern.search(text):
                    raise RetirementBoundaryError(
                        "current production/release path references retired "
                        f"Python application: {path}"
                    )


def _validate_assets() -> dict[str, Any]:
    document = _read_json(MANIFEST)
    if document.get("manifest_version") != "m12.runtime-assets.v1":
        raise RetirementBoundaryError("unexpected runtime asset manifest version")
    if (
        document.get("migration_count") != 54
        or document.get("schema_change") is not False
    ):
        raise RetirementBoundaryError("runtime manifest does not preserve schema 54")
    if document.get("source_reference_commit") != (
        "c6b5d2c25038a8ac155c71f68fd50afea03fa459"
    ):
        raise RetirementBoundaryError(
            "runtime manifest changed the frozen source commit"
        )
    if document.get("source_reference_tree") != (
        "2887b6b6a3be38ad8b386781cda76f888d5ac0dc"
    ):
        raise RetirementBoundaryError("runtime manifest changed the frozen source tree")

    raw_assets = document.get("assets")
    if not isinstance(raw_assets, list) or not raw_assets:
        raise RetirementBoundaryError("runtime asset manifest is empty")
    assets = cast("list[object]", raw_assets)
    categories: set[str] = set()
    for raw_asset in assets:
        if not isinstance(raw_asset, dict):
            raise RetirementBoundaryError("runtime asset entry is malformed")
        asset = cast("dict[str, object]", raw_asset)
        relative_path = asset.get("path")
        expected = asset.get("sha256")
        category = asset.get("category")
        if not isinstance(relative_path, str) or not isinstance(expected, str):
            raise RetirementBoundaryError("runtime asset entry is malformed")
        if not isinstance(category, str):
            raise RetirementBoundaryError("runtime asset category is missing")
        path = ROOT / "rust/assets" / relative_path
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            raise RetirementBoundaryError(
                f"Rust-owned asset is missing: {relative_path}"
            ) from error
        if actual != expected:
            raise RetirementBoundaryError(f"Rust-owned asset drift: {relative_path}")
        categories.add(category)

    required = {
        "release-catalog",
        "configuration-template",
        "dashboard-assets",
        "provider-templates",
        "sqlite-migrations",
        "wire-profiles",
    }
    if categories != required:
        raise RetirementBoundaryError(
            f"runtime asset categories differ: {sorted(categories)}"
        )

    checksum_path = ROOT / "rust/assets/db/migrations/checksums.json"
    checksums = _read_json(checksum_path)
    raw_files = checksums.get("files")
    if not isinstance(raw_files, dict):
        raise RetirementBoundaryError(
            "Rust-owned migration checksum inventory is incomplete"
        )
    files = cast("dict[str, object]", raw_files)
    if len(files) != 54:
        raise RetirementBoundaryError(
            "Rust-owned migration checksum inventory is incomplete"
        )
    migration_paths = sorted((ROOT / "rust/assets/db/migrations").glob("*.sql"))
    if len(migration_paths) != 54:
        raise RetirementBoundaryError("Rust-owned migration inventory is not schema 54")
    for path in migration_paths:
        expected = files.get(path.name)
        if not isinstance(expected, str):
            raise RetirementBoundaryError(
                f"migration is absent from checksums: {path.name}"
            )
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise RetirementBoundaryError(f"migration checksum drift: {path.name}")
    return {"status": "pass", "asset_count": len(assets), "migration_count": 54}


def validate_retirement_boundary() -> dict[str, Any]:
    _validate_references()
    return _validate_assets()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        print(json.dumps(validate_retirement_boundary(), sort_keys=True))
    except (OSError, RetirementBoundaryError) as error:
        print(f"M12 retirement boundary validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
