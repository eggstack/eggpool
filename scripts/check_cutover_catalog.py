#!/usr/bin/env python3
"""Validate the frozen K001 release catalog and version authorities.

This is a contract checker, not an updater.  It intentionally treats the
root Hatchling project as the historical Python oracle and Cargo as the Rust
candidate authority while the cutover version is still reserved.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "migration-rs/fixtures/cutover/k001-installable-releases.json"
_VERSION_RE = re.compile(
    r"(?P<release>\d+(?:\.\d+){2})(?P<suffix>(?:a|b|rc|dev|post)\d*)?\Z"
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")


class CatalogError(ValueError):
    """A stable, secret-free catalog validation error."""


def normalize_version(raw: str) -> str:
    """Normalize one accepted EggPool release spelling."""

    value = raw.strip()
    if value[:1].lower() == "v":
        value = value[1:]
    if not _VERSION_RE.fullmatch(value):
        raise CatalogError("invalid EggPool release version")
    return value


def _version_key(raw: str) -> tuple[int, int, int, int, int]:
    value = normalize_version(raw)
    match = _VERSION_RE.fullmatch(value)
    if match is None:  # pragma: no cover - guarded by normalize_version
        raise CatalogError("invalid EggPool release version")
    release_parts = tuple(int(part) for part in match.group("release").split("."))
    suffix = match.group("suffix") or ""
    kind = re.match(r"[a-z]+", suffix)
    rank = {"dev": 0, "a": 1, "b": 2, "rc": 3, "": 4, "post": 5}.get(
        kind.group(0) if kind else "", -1
    )
    number = (
        int(suffix[len(kind.group(0)) :])
        if kind and suffix[len(kind.group(0)) :]
        else 0
    )
    return (release_parts[0], release_parts[1], release_parts[2], rank, number)


def _as_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CatalogError(f"{label} must be an object")
    return cast("dict[str, Any]", value)


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CatalogError(f"{label} must be a non-empty string")
    return value


def _walk_for_secrets(value: object, path: str = "catalog") -> None:
    forbidden_keys = (
        "api_key",
        "authorization",
        "credential",
        "password",
        "secret",
        "token",
    )
    forbidden_values = ("-----begin ", "sk-", "bearer ")
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        for key, nested in mapping.items():
            key_text = str(key).lower()
            if any(fragment in key_text for fragment in forbidden_keys):
                raise CatalogError(f"secret-bearing field is forbidden: {path}.{key}")
            _walk_for_secrets(nested, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sequence = cast("Sequence[object]", value)
        for index, nested in enumerate(sequence):
            _walk_for_secrets(nested, f"{path}[{index}]")
    elif isinstance(value, str):
        lowered = value.lower()
        if any(fragment in lowered for fragment in forbidden_values):
            raise CatalogError(f"secret-bearing value is forbidden: {path}")


def expanded_release(
    catalog: Mapping[str, Any], release: Mapping[str, Any]
) -> dict[str, Any]:
    """Resolve the catalog's documented shared release defaults."""

    defaults = _as_mapping(catalog.get("release_defaults"), "release_defaults")
    result = dict(defaults)
    result.update(release)
    version = _require_string(result.get("version"), "release.version")
    result["version"] = normalize_version(version)
    expected_version = _require_string(
        result.get("expected_version", version), "release.expected_version"
    ).replace("{version}", result["version"])
    result["expected_version"] = normalize_version(expected_version)
    requirement = _require_string(
        result.get("package_manager_requirement", ""),
        "release.package_manager_requirement",
    )
    result["package_manager_requirement"] = requirement.replace(
        "{version}", result["version"]
    )
    return result


def validate_catalog(catalog: Mapping[str, Any]) -> dict[str, int | str]:
    """Validate K001's machine-readable contract and return stable counts."""

    _walk_for_secrets(catalog)
    if catalog.get("catalog_version") != "k001.v1":
        raise CatalogError("unsupported K001 catalog version")
    if catalog.get("package_name") != "eggpool":
        raise CatalogError("catalog package name must be eggpool")
    if catalog.get("artifact_stage") not in {"not-active", "active"}:
        raise CatalogError("artifact stage must be explicit")

    targets = _as_mapping(catalog.get("target_matrix"), "target_matrix")
    qualified_targets = {
        name
        for name, value in targets.items()
        if _as_mapping(value, f"target_matrix.{name}").get("classification")
        in {"supported", "supported-development"}
    }
    if qualified_targets != {"linux-x86_64", "linux-aarch64", "macos-arm64"}:
        raise CatalogError("K001 target matrix does not match M10")
    for name, value in targets.items():
        target = _as_mapping(value, f"target_matrix.{name}")
        _require_string(target.get("rust_target"), f"target_matrix.{name}.rust_target")
        tags = target.get("wheel_platform_tags")
        if not isinstance(tags, list) or (
            not tags
            and target.get("classification") in {"supported", "supported-development"}
        ):
            raise CatalogError(
                f"target_matrix.{name}.wheel_platform_tags must be non-empty"
            )
        if (
            target.get("classification") not in {"supported", "supported-development"}
            and tags != []
        ):
            raise CatalogError(f"unsupported target {name} cannot have wheel tags")

    authority = _as_mapping(catalog.get("version_authority"), "version_authority")
    cutover = normalize_version(
        _require_string(authority.get("cutover_version"), "cutover_version")
    )
    if authority.get("phase") != "reserved":
        raise CatalogError("K001 must freeze a reserved, not published, cutover")
    for field in ("historical_python_project_version", "rust_cargo_version"):
        normalize_version(
            _require_string(authority.get(field), f"version_authority.{field}")
        )
    if (
        authority["historical_python_project_version"]
        != authority["rust_cargo_version"]
    ):
        raise CatalogError("historical Python and current Rust versions disagree")
    if authority.get("cutover_tag") != f"v{cutover}":
        raise CatalogError(
            "cutover tag must be the normalized version with a leading v"
        )
    if authority.get("cutover_source_commit") is not None:
        raise CatalogError("reserved cutover cannot claim a source commit")

    releases_value = catalog.get("releases")
    if not isinstance(releases_value, list) or not releases_value:
        raise CatalogError("catalog releases must be a non-empty array")
    raw_releases = cast("list[object]", releases_value)
    releases = [_as_mapping(item, "release entry") for item in raw_releases]
    seen: set[str] = set()
    for raw_release in releases:
        release = expanded_release(catalog, raw_release)
        version = release["version"]
        if version in seen:
            raise CatalogError(f"duplicate release version: {version}")
        seen.add(version)
        if _version_key(version) >= _version_key(cutover):
            raise CatalogError(f"cutover is not newer than release {version}")
        if release.get("implementation_era") not in {"python", "rust"}:
            raise CatalogError(f"invalid implementation era for {version}")
        if release.get("source_tag") != f"v{version}":
            raise CatalogError(f"source tag does not match {version}")
        commit = _require_string(
            release.get("source_commit"), f"source commit for {version}"
        )
        if not _COMMIT_RE.fullmatch(commit):
            raise CatalogError(f"source commit is not immutable for {version}")
        if release.get("public_release_status") not in {
            "published",
            "yanked",
            "unavailable",
        }:
            raise CatalogError(f"invalid public status for {version}")
        if not isinstance(release.get("pypi_presence"), bool):
            raise CatalogError(f"PyPI presence must be explicit for {version}")
        files = release.get("pypi_files")
        if release["pypi_presence"] and (not isinstance(files, list) or not files):
            raise CatalogError(f"published PyPI release has no files: {version}")
        if files is not None:
            if not isinstance(files, list):
                raise CatalogError(f"PyPI files must be an array for {version}")
            filenames: set[str] = set()
            raw_files = cast("list[object]", files)
            for raw_file in raw_files:
                file_info = _as_mapping(raw_file, f"PyPI file for {version}")
                filename = _require_string(file_info.get("filename"), "PyPI filename")
                if filename in filenames or not filename.startswith(
                    f"eggpool-{version}"
                ):
                    raise CatalogError(
                        f"invalid or duplicate PyPI filename for {version}"
                    )
                filenames.add(filename)
                digest = _require_string(
                    file_info.get("sha256"), f"PyPI hash for {filename}"
                )
                if not _SHA256_RE.fullmatch(digest):
                    raise CatalogError(f"invalid PyPI SHA-256 for {filename}")
        supported = release.get("supported_target_classes")
        if (
            not isinstance(supported, list)
            or not set(cast("list[str]", supported)) <= qualified_targets
        ):
            raise CatalogError(f"unsupported target appears in release {version}")
        if release.get("expected_version") != version:
            raise CatalogError(f"expected runtime version does not match {version}")
        if not isinstance(release.get("yanked"), bool) or not isinstance(
            release.get("unavailable"), bool
        ):
            raise CatalogError(
                f"yanked/unavailable state must be explicit for {version}"
            )
        if release.get("public_release_status") == "published" and (
            release["yanked"] or release["unavailable"]
        ):
            raise CatalogError(
                f"published release has invalid unavailable state: {version}"
            )
        if (
            catalog["artifact_stage"] == "active"
            and release["implementation_era"] == "rust"
        ):
            wheels = release.get("supported_wheels")
            if (
                not isinstance(wheels, Mapping)
                or set(cast("Mapping[str, object]", wheels)) != qualified_targets
            ):
                raise CatalogError(
                    f"Rust release has incomplete supported wheels: {version}"
                )
        _require_string(
            release.get("db_config_compatibility"), f"compatibility for {version}"
        )
        _require_string(
            release.get("rollback_suitability"), f"rollback status for {version}"
        )

    inventory = _as_mapping(catalog.get("official_inventory"), "official_inventory")
    if inventory.get("github_stable_release_count") != len(releases):
        raise CatalogError("GitHub stable release count does not match catalog")
    if inventory.get("pypi_stable_release_count") != len(releases):
        raise CatalogError("PyPI stable release count does not match catalog")
    if inventory.get("missing_pypi_versions") != []:
        raise CatalogError(
            "current catalog must record that no PyPI versions are missing"
        )
    if _version_key(cutover) <= _version_key(
        _require_string(inventory.get("latest_stable_version"), "latest version")
    ):
        raise CatalogError("cutover must be newer than latest stable release")

    rollback = _as_mapping(catalog.get("rollback_window"), "rollback_window")
    compatible_versions = rollback.get("compatible_versions")
    if not isinstance(compatible_versions, list) or not compatible_versions:
        raise CatalogError("rollback window must enumerate compatible versions")
    compatible_version_values = cast("list[object]", compatible_versions)
    for version in compatible_version_values:
        normalized = normalize_version(_require_string(version, "rollback version"))
        if normalized not in seen:
            raise CatalogError(f"rollback version is absent from catalog: {normalized}")
    return {
        "catalog_version": "k001.v1",
        "cutover_version": cutover,
        "release_count": len(releases),
        "rollback_count": len(compatible_version_values),
    }


def check_version_authorities(repo_root: Path, catalog: Mapping[str, Any]) -> None:
    """Check the current side-by-side source versions against K001 metadata."""

    authority = _as_mapping(catalog["version_authority"], "version_authority")
    try:
        with (repo_root / "pyproject.toml").open("rb") as handle:
            python_project = tomllib.load(handle)
        with (repo_root / "rust/Cargo.toml").open("rb") as handle:
            rust_project = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CatalogError("version authority source could not be read") from exc
    python_version = python_project.get("project", {}).get("version")
    rust_version = rust_project.get("package", {}).get("version")
    if python_version != authority["historical_python_project_version"]:
        raise CatalogError("root Python oracle version disagrees with K001")
    if rust_version != authority["rust_cargo_version"]:
        raise CatalogError("Rust Cargo version disagrees with K001")
    if (
        authority["phase"] == "reserved"
        and python_version == authority["cutover_version"]
    ):
        raise CatalogError("reserved cutover has already replaced the Python oracle")


def load_and_validate(path: Path = DEFAULT_CATALOG) -> dict[str, Any]:
    """Load and validate a catalog JSON document."""

    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError("catalog JSON could not be read") from exc
    catalog = _as_mapping(value, "catalog")
    validate_catalog(catalog)
    return catalog


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        catalog = load_and_validate(args.catalog)
        check_version_authorities(args.repo_root, catalog)
    except CatalogError as exc:
        print(f"K001 catalog invalid: {exc}", file=sys.stderr)
        return 1
    summary = validate_catalog(catalog)
    print(
        f"K001 catalog valid: {summary['release_count']} releases; "
        f"cutover {summary['cutover_version']} reserved; "
        f"{summary['rollback_count']} rollback-compatible"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
