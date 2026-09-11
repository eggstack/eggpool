"""Deterministic K008 release workflow and publication contract tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.validate_cutover_release import validate_candidate
from scripts.validate_release_workflow import (
    WorkflowValidationError,
    validate_workflow,
    validate_workflow_text,
)
from scripts.verify_published_release import (
    PublicationVerificationError,
    verify_publication,
)

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github/workflows/release.yml"
MANIFEST = ROOT / "migration-rs/closure/cutover/k003-release-manifest.json"


def _manifest() -> dict[str, object]:
    value = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _public_metadata() -> tuple[dict[str, object], dict[str, object]]:
    manifest = _manifest()
    records = manifest["artifacts"]
    assert isinstance(records, list)
    pypi_urls = []
    github_assets = [
        {"name": "SHA256SUMS"},
        {"name": "eggpool-0.8.0-release-manifest.json"},
    ]
    for record in records:
        assert isinstance(record, dict)
        wheel = record["wheel"]
        raw = record["raw"]
        assert isinstance(wheel, dict)
        assert isinstance(raw, dict)
        pypi_urls.append(
            {"filename": wheel["filename"], "digests": {"sha256": wheel["sha256"]}}
        )
        github_assets.append(
            {"name": raw["filename"], "digest": f"sha256:{raw['sha256']}"}
        )
    pypi = {"info": {"version": "0.8.0"}, "urls": pypi_urls}
    github = {
        "tag_name": "v0.8.0",
        "draft": False,
        "prerelease": False,
        "assets": github_assets,
    }
    return pypi, github


def test_workflow_has_immutable_jobs_and_separate_publish_authority() -> None:
    summary = validate_workflow(WORKFLOW)
    assert summary["status"] == "pass"
    assert summary["targets"] == ["linux-x86_64", "linux-aarch64", "macos-arm64"]
    assert len(summary["jobs"]) == 9


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda text: text.replace(
                "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5",
                "actions/checkout@v4",
                1,
            ),
            "immutable SHA",
        ),
        (
            lambda text: text.replace(
                "attestations: true", "password: ${{ secrets.PYPI_TOKEN }}", 1
            ),
            "secret",
        ),
        (
            lambda text: text.replace(
                "--target-class macos-arm64", "--target-class windows-x86_64", 1
            ),
            "unsupported/universal",
        ),
        (
            lambda text: text.replace(
                "repository-url: https://test.pypi.org/legacy/",
                "repository-url: https://pypi.org/legacy/",
                1,
            ),
            "TestPyPI URL",
        ),
    ],
)
def test_workflow_rejects_supply_chain_contract_mutations(
    mutation: object, message: str
) -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    mutated = mutation(text)  # type: ignore[operator]
    with pytest.raises(WorkflowValidationError, match=message):
        validate_workflow_text(mutated)


def test_candidate_identity_matches_cargo_and_packaging_contract() -> None:
    result = validate_candidate()
    assert result["status"] == "pass"
    assert result["version"] == "0.8.0"


def test_publication_verifier_accepts_exact_manifest_bytes() -> None:
    pypi, github = _public_metadata()
    result = verify_publication(_manifest(), pypi, github)
    assert result == {
        "status": "pass",
        "version": "0.8.0",
        "wheels": 3,
        "raw_assets": 3,
    }


def test_publication_verifier_rejects_missing_github_digest() -> None:
    pypi, github = _public_metadata()
    changed = copy.deepcopy(github)
    assets = changed["assets"]
    assert isinstance(assets, list)
    assets[2].pop("digest")
    with pytest.raises(PublicationVerificationError, match="digest"):
        verify_publication(_manifest(), pypi, changed)


def test_publication_verifier_rejects_source_archives() -> None:
    pypi, github = _public_metadata()
    pypi["urls"].append(
        {"filename": "eggpool-0.8.0.tar.gz", "digests": {"sha256": "0" * 64}}
    )
    with pytest.raises(PublicationVerificationError, match="source archive"):
        verify_publication(_manifest(), pypi, github)
