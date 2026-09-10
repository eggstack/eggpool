"""Deterministic contract tests for the K001 cutover catalog."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from scripts.check_cutover_catalog import (
    CatalogError,
    expanded_release,
    load_and_validate,
    normalize_version,
    validate_catalog,
)

ROOT = Path(__file__).parents[2]
CATALOG_PATH = ROOT / "migration-rs/fixtures/cutover/k001-installable-releases.json"


def catalog() -> dict[str, Any]:
    with CATALOG_PATH.open(encoding="utf-8") as handle:
        value = json.load(handle)
    assert isinstance(value, dict)
    return value


def releases(value: dict[str, Any]) -> list[dict[str, Any]]:
    raw = value["releases"]
    assert isinstance(raw, list)
    return raw


def test_frozen_catalog_is_complete_and_current() -> None:
    value = load_and_validate(CATALOG_PATH)
    summary = validate_catalog(value)

    assert summary == {
        "catalog_version": "k001.v1",
        "cutover_version": "0.8.0",
        "release_count": 56,
        "rollback_count": 8,
    }
    assert value["official_inventory"]["missing_pypi_versions"] == []
    assert all(
        expanded_release(value, release)["implementation_era"] == "python"
        for release in releases(value)
    )
    assert {
        release["version"]
        for release in releases(value)
        if release["rollback_suitability"] == "compatible-with-schema54"
    } == set(value["rollback_window"]["compatible_versions"])


def test_leading_v_normalization_is_stable() -> None:
    assert normalize_version(" v0.7.4 ") == "0.7.4"
    assert normalize_version("0.8.0") == "0.8.0"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("duplicate", "duplicate release version"),
        ("mutable-source", "source commit is not immutable"),
        ("missing-source", "source commit for"),
        ("unsupported-target", "unsupported target appears"),
        ("secret", "secret-bearing field"),
    ],
)
def test_catalog_rejects_contract_breaking_mutations(
    mutation: str, message: str
) -> None:
    value = deepcopy(catalog())
    first = releases(value)[0]
    if mutation == "duplicate":
        releases(value).append(deepcopy(first))
    elif mutation == "mutable-source":
        first["source_commit"] = "main"
    elif mutation == "missing-source":
        first.pop("source_commit")
    elif mutation == "unsupported-target":
        first["supported_target_classes"] = ["windows"]
    elif mutation == "secret":
        value["unexpected_api_key"] = "placeholder"

    with pytest.raises(CatalogError, match=message):
        validate_catalog(value)


def test_yanked_state_is_explicit_and_representable() -> None:
    value = deepcopy(catalog())
    first = releases(value)[0]
    first["public_release_status"] = "yanked"
    first["yanked"] = True
    validate_catalog(value)


def test_rust_release_requires_wheels_after_artifact_stage_activation() -> None:
    value = deepcopy(catalog())
    value["artifact_stage"] = "active"
    releases(value)[-1]["implementation_era"] = "rust"

    with pytest.raises(CatalogError, match="incomplete supported wheels"):
        validate_catalog(value)


def test_reserved_cutover_has_no_source_or_public_artifact() -> None:
    value = catalog()
    authority = value["version_authority"]
    assert authority["phase"] == "reserved"
    assert authority["cutover_source_commit"] is None
    versions = {release["version"] for release in releases(value)}
    assert authority["cutover_version"] not in versions
