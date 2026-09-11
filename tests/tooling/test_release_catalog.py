"""Deterministic contract tests for the installable release catalog."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from scripts.check_release_catalog import (
    CatalogError,
    expanded_release,
    load_and_validate,
    normalize_version,
    validate_catalog,
)

ROOT = Path(__file__).parents[2]
CATALOG_PATH = ROOT / "rust/assets/catalog/installable-releases.json"


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
        "catalog_version": "release-catalog.v1",
        "native_release_version": "0.8.0",
        "release_count": 57,
        "rollback_count": 8,
    }
    assert value["official_inventory"]["missing_pypi_versions"] == []
    assert all(
        expanded_release(value, release)["implementation_era"] == "python"
        for release in releases(value)[:-1]
    )
    assert expanded_release(value, releases(value)[-1])["implementation_era"] == "rust"
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
    releases(value)[-1].pop("supported_wheels")

    with pytest.raises(CatalogError, match="incomplete supported wheels"):
        validate_catalog(value)


def test_published_native_release_has_immutable_source_and_public_artifact() -> None:
    value = catalog()
    authority = value["version_authority"]
    assert authority["publication_status"] == "published"
    assert authority["native_release_source_commit"]
    assert authority["rust_cargo_version"] == authority["native_release_version"]
    versions = {release["version"] for release in releases(value)}
    assert authority["native_release_version"] in versions
    assert releases(value)[-1]["implementation_era"] == "rust"
    assert releases(value)[-1]["pypi_files"]
