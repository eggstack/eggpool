"""Guards for derived release versions in the release-related workflows.

The release workflow already derived its version from the repository's
version authority. These tests keep the two qualification workflows on the
same derivation, keep a hardcoded EggPool version literal out of every
covered workflow, and keep every covered workflow pinned to immutable action
SHAs.
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest

from scripts.inspect_release_raw import raw_filename
from scripts.validate_release_identity import validate_release_identity
from scripts.validate_release_workflow import (
    COVERED_WORKFLOWS,
    WorkflowValidationError,
    catalog_release_versions,
    job_blocks,
    validate_covered_workflow,
    validate_qualification_workflow_text,
    validate_version_derivation,
)

ROOT = Path(__file__).parents[2]
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
CATALOG = ROOT / "rust/assets/catalog/installable-releases.json"
QUALIFICATION_WORKFLOWS = tuple(
    relative
    for relative, contract in COVERED_WORKFLOWS.items()
    if contract == "qualification"
)
# The artifact selectors the workflows use, with the version left open: one
# selects the wheel, the others select the raw executables.
ARTIFACT_SELECTORS = ("-*.whl", "-linux-*", "-macos-*")
# The derived-version artifact glob a workflow step must carry: the version
# comes from the resolved job output, never from a literal.
DERIVED_ARTIFACT_GLOB = re.compile(
    r"eggpool-(?:\$\{\{\s*needs\.[A-Za-z0-9_-]+\.outputs\.version\s*\}\}"
    r"|\$\{[A-Z][A-Z0-9_]*VERSION\})-[^\s'\"]*"
)


def workflow_text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def cargo_version() -> str:
    manifest = tomllib.loads((ROOT / "rust/Cargo.toml").read_text(encoding="utf-8"))
    package = manifest["package"]
    assert isinstance(package, dict)
    return str(package["version"])


def tracked_files() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return set(result.stdout.splitlines())


def catalog_release(version: str) -> dict[str, Any]:
    """Return the release catalog entry for one release version."""
    catalog = cast("dict[str, Any]", json.loads(CATALOG.read_text(encoding="utf-8")))
    for entry in cast("list[dict[str, Any]]", catalog.get("releases", [])):
        if entry.get("version") == version:
            return entry
    raise AssertionError(f"the release catalog has no entry for {version}")


def release_artifacts(version: str) -> list[str]:
    """Return the artifact filenames the release build produces for a version.

    The wheel names are the catalog's own published filenames and the raw
    executable names come from the repository's raw artifact naming contract,
    so the names are the ones the build actually produces rather than names
    invented by the test.
    """
    entry = catalog_release(version)
    targets = cast("list[str]", entry.get("supported_target_classes", []))
    assert targets, "the release catalog must name the supported target classes"
    wheels = cast("dict[str, str]", entry.get("supported_wheels", {}))
    assert wheels, "the release catalog must describe the published wheels"
    names = [wheels[target] for target in targets]
    names.extend(raw_filename(version, target) for target in targets)
    return names


def workflow_target_classes(text: str) -> set[str]:
    """Return the target classes the workflow's own matrix builds."""
    return set(re.findall(r"target_class:\s*([a-z0-9_-]+)", text)) | set(
        re.findall(r"--target-class\s+([a-z0-9_-]+)", text)
    )


def build_jobs(text: str) -> dict[str, str]:
    """Return the jobs that build the release artifacts."""
    return {
        job: block
        for job, block in job_blocks(text).items()
        if "scripts/build_release_artifacts.py" in block
    }


def test_catalog_is_the_version_authority_for_the_literal_guard() -> None:
    versions = catalog_release_versions()
    assert cargo_version() in versions
    assert len(versions) >= 2


def test_no_covered_workflow_hardcodes_an_eggpool_release_version() -> None:
    versions = catalog_release_versions()
    literal = re.compile(r"(?<![0-9.])\d+\.\d+\.\d+(?![0-9.])")
    offenders: dict[str, list[str]] = {}
    for relative in COVERED_WORKFLOWS:
        found = sorted(
            {
                value
                for value in literal.findall(workflow_text(relative))
                if value in versions
            }
        )
        if found:
            offenders[relative] = found
    assert offenders == {}


def test_every_covered_workflow_passes_its_own_contract() -> None:
    for relative in COVERED_WORKFLOWS:
        summary = validate_covered_workflow(ROOT / relative)
        assert summary["status"] == "pass"
        assert summary["workflow"] == relative


def test_qualification_workflows_derive_the_version_from_the_authority() -> None:
    assert len(QUALIFICATION_WORKFLOWS) == 2
    for relative in QUALIFICATION_WORKFLOWS:
        text = workflow_text(relative)
        assert "validate_release_identity.py" in text
        assert "GITHUB_OUTPUT" in text
        assert "outputs.version" in text


def test_derived_version_matches_the_cargo_version() -> None:
    result = validate_release_identity()
    assert result["version"] == cargo_version()


def test_stale_version_globs_do_not_match_the_current_artifacts() -> None:
    """A catalogued version literal must not survive in any covered glob.

    The property is proved against the artifacts the build actually produces
    at the repository's current version: the catalog's published wheel names
    and the raw executable names of the repository's naming contract. The
    derived glob must select every one of them and a glob built from any
    superseded catalogued version must select none of them.
    """
    version = cargo_version()
    stale_versions = sorted(catalog_release_versions() - {version})
    assert stale_versions, "the catalog must hold at least one superseded version"

    artifacts = release_artifacts(version)
    targets = set(catalog_release(version).get("supported_target_classes", []))
    # One wheel and one raw executable per supported target class.
    assert len(artifacts) == 2 * len(targets), artifacts
    for relative in COVERED_WORKFLOWS:
        assert targets <= workflow_target_classes(workflow_text(relative)), relative

    # No covered workflow selects an artifact with a catalogued literal.
    for relative in COVERED_WORKFLOWS:
        text = workflow_text(relative)
        for stale in stale_versions:
            assert f"eggpool-{stale}-" not in text, (
                f"{relative} selects artifacts with the stale version {stale}"
            )

    # Every build job selects its artifacts with the derived version, and
    # every covered workflow carries at least one derived artifact glob.
    for relative in COVERED_WORKFLOWS:
        text = workflow_text(relative)
        blocks = job_blocks(text)
        derived_jobs = {
            job
            for job, block in blocks.items()
            if DERIVED_ARTIFACT_GLOB.search(block) is not None
        }
        assert derived_jobs, f"{relative} carries no derived artifact glob"
        for job, block in build_jobs(text).items():
            globs = DERIVED_ARTIFACT_GLOB.findall(block)
            assert globs, f"{relative}:{job} selects artifacts without a version"
            assert any(glob.endswith("-*.whl") for glob in globs), (
                f"{relative}:{job} does not select its wheel with the derived version"
            )

    # The derived glob selects every artifact the build produces, and a glob
    # built from a superseded catalogued version selects none of them.
    for selector in ARTIFACT_SELECTORS:
        derived_glob = f"eggpool-{version}{selector}"
        selected = [
            name for name in artifacts if fnmatch.fnmatchcase(name, derived_glob)
        ]
        assert selected, derived_glob
        for stale in stale_versions:
            stale_glob = f"eggpool-{stale}{selector}"
            assert not [
                name for name in artifacts if fnmatch.fnmatchcase(name, stale_glob)
            ], stale_glob
    covered = {
        name
        for selector in ARTIFACT_SELECTORS
        for name in artifacts
        if fnmatch.fnmatchcase(name, f"eggpool-{version}{selector}")
    }
    assert covered == set(artifacts)


@pytest.mark.parametrize("relative", sorted(COVERED_WORKFLOWS))
def test_validator_rejects_a_reintroduced_version_literal(relative: str) -> None:
    text = workflow_text(relative)
    stale = "0.8.0"
    assert stale not in text
    mutated = text.replace("eggpool-", f"eggpool-{stale}-", 1)
    assert stale in mutated
    with pytest.raises(WorkflowValidationError, match="hardcodes the EggPool release"):
        validate_version_derivation(mutated)


@pytest.mark.parametrize("relative", sorted(QUALIFICATION_WORKFLOWS))
def test_validator_rejects_a_tag_style_action_pin(relative: str) -> None:
    """A tag-style action reference must be rejected by the contract."""
    text = workflow_text(relative)
    pinned = re.search(r"uses:\s*([A-Za-z0-9_.\-/]+)@([0-9a-f]{40})", text)
    assert pinned is not None, relative
    mutated = text.replace(pinned.group(0), f"uses: {pinned.group(1)}@v4", 1)
    assert f"{pinned.group(1)}@v4" in mutated
    with pytest.raises(WorkflowValidationError, match="not pinned to an immutable SHA"):
        validate_qualification_workflow_text(mutated)


@pytest.mark.parametrize("relative", sorted(QUALIFICATION_WORKFLOWS))
def test_validator_rejects_a_workflow_without_the_version_gate(relative: str) -> None:
    text = workflow_text(relative)
    mutated = text.replace("validate_release_identity.py", "print_version.py")
    assert "validate_release_identity.py" not in mutated
    with pytest.raises(WorkflowValidationError, match="version gate is missing"):
        validate_qualification_workflow_text(mutated)


def test_validator_rejects_a_workflow_that_omits_the_version_resolution_job() -> None:
    relative = ".github/workflows/qualification-release-artifacts.yml"
    text = workflow_text(relative)
    start = text.index("  resolve-version:")
    end = text.index("  build-and-qualify:")
    mutated = text[:start] + text[end:]
    with pytest.raises(
        WorkflowValidationError, match="version resolution job is missing"
    ):
        validate_qualification_workflow_text(mutated)


def test_validator_rejects_a_workflow_outside_the_covered_set() -> None:
    with pytest.raises(WorkflowValidationError, match="not a covered release workflow"):
        validate_covered_workflow(ROOT / ".github/workflows/ci.yml")


def test_ci_runs_the_release_workflow_validator_for_every_covered_workflow() -> None:
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "scripts/validate_release_workflow.py" in text
    for relative in COVERED_WORKFLOWS:
        assert relative in text


def test_covered_workflows_are_tracked() -> None:
    tracked = tracked_files()
    for relative in COVERED_WORKFLOWS:
        assert relative in tracked
