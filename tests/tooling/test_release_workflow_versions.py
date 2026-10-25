"""Guards for derived release versions and CI lock discipline.

The release workflow already derived its version from the repository's
version authority. These tests keep the two qualification workflows on the
same derivation, keep a hardcoded EggPool version literal out of every
covered workflow, and keep every covered workflow pinned to immutable action
SHAs.

They also keep the CI environment on the committed lockfile. A workflow that
installs the project environment must install it with `uv sync --locked`, so a
lockfile that has drifted from the project metadata fails the job at the gate
instead of being silently re-resolved and rewritten by the first `uv run` that
follows it.
"""

from __future__ import annotations

import ast
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
RELEASE_DOCS_TEST = ROOT / "tests/tooling/test_release_docs.py"
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
# The four CI gates that install the repository's Python environment. The
# guard derives the project-installing set from the workflow text, so a
# workflow that installs the environment outside these four is covered
# without a manual edit; this tuple is what keeps the guard non-vacuous.
LOCK_GATED_WORKFLOWS = (
    ".github/workflows/ci.yml",
    ".github/workflows/installer-shell-qualification.yml",
    ".github/workflows/qualification-portability.yml",
    ".github/workflows/qualification-release-artifacts.yml",
)
# The release workflow's isolation. `uv run --no-project` consults neither the
# project nor its lockfile, so the release workflow is not a project-
# environment installer and must not be swept into that set.
RELEASE_WORKFLOW = ".github/workflows/release.yml"
RELEASE_ISOLATION = "uv run --no-project --with maturin==1.14.1"
RELEASE_ISOLATION_SITES = 8
# A `run:` step in a workflow file. The command is either inline after the
# key or in a block scalar the key owns.
RUN_KEY = re.compile(r"^(\s*)(?:-\s+)?run:\s*(.*)$")
# A YAML block scalar indicator: `|`, `>`, and their chomping and
# indentation variants.
BLOCK_SCALAR = re.compile(r"[|>][+-]?\d*")
# The command that installs the repository's project environment from the
# committed lockfile. `uv run --no-project` installs no project environment,
# and `uv pip install` targets a different environment, so neither matches.
PROJECT_SYNC = re.compile(r"(?<![A-Za-z0-9_-])uv[ \t]+sync(?![A-Za-z0-9_-])")


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


def workflow_paths() -> list[str]:
    """Return every workflow file in the repository, as relative paths."""
    return sorted(
        f".github/workflows/{path.name}"
        for path in (ROOT / ".github/workflows").iterdir()
        if path.suffix in {".yml", ".yaml"}
    )


def workflow_run_steps(text: str) -> list[str]:
    """Return the command text of every `run:` step in a workflow.

    The workflow files are read as text, so the guard needs no YAML runtime
    dependency. A step's command is the inline value of its `run:` key plus
    every continuation line that key owns, which covers the single-line,
    folded (`>-`), and literal (`|`) forms the repository's workflows use.
    """
    lines = text.splitlines()
    steps: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if RUN_KEY.match(line) is None:
            index += 1
            continue
        key_column = line.index("run:")
        inline = line[key_column + len("run:") :].strip()
        if BLOCK_SCALAR.fullmatch(inline) is None:
            steps.append(inline)
            index += 1
            continue
        index += 1
        body: list[str] = []
        while index < len(lines):
            candidate = lines[index]
            if not candidate.strip():
                body.append("")
                index += 1
                continue
            if len(candidate) - len(candidate.lstrip()) <= key_column:
                break
            body.append(candidate.strip())
            index += 1
        steps.append("\n".join(body))
    return steps


def project_environment_installs(relative: str) -> list[str]:
    """Return the project-environment install commands a workflow runs.

    Only `uv sync` installs the repository's project environment from the
    committed lockfile. A workflow that installs the environment through some
    other mechanism has no `uv sync` command here, so it is not reported and
    the guard does not fail it spuriously.
    """
    return [
        step
        for step in workflow_run_steps(workflow_text(relative))
        if PROJECT_SYNC.search(step)
    ]


def project_installing_workflows() -> dict[str, list[str]]:
    """Return the workflows that install the project environment."""
    installs = {
        relative: project_environment_installs(relative)
        for relative in workflow_paths()
    }
    return {relative: steps for relative, steps in installs.items() if steps}


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


def test_no_workflow_installs_the_project_environment_with_frozen() -> None:
    """`uv sync --frozen` installs the recorded versions without checking.

    The flag skips the check that the lockfile still satisfies the project
    metadata, so the job would install a stale resolution and the first
    `uv run` that follows would re-resolve and rewrite the lockfile. No
    workflow may carry it.
    """
    offenders = {
        relative: [
            line.strip()
            for line in workflow_text(relative).splitlines()
            if "uv sync --frozen" in line
        ]
        for relative in workflow_paths()
    }
    assert {relative: lines for relative, lines in offenders.items() if lines} == {}


def test_every_project_environment_install_uses_the_locked_lockfile() -> None:
    """The project environment must come from the committed lockfile.

    `uv sync --locked` installs exactly what the lockfile records and fails
    when the lockfile no longer satisfies the project metadata, so the drift
    is reported at the gate instead of being silently re-resolved.
    """
    installing = project_installing_workflows()
    # The guard must not be vacuous: the four corrected gates are present.
    missing = sorted(set(LOCK_GATED_WORKFLOWS) - set(installing))
    assert missing == [], missing
    offenders = {
        relative: [step for step in steps if "--locked" not in step]
        for relative, steps in installing.items()
    }
    assert {relative: steps for relative, steps in offenders.items() if steps} == {}


def test_the_release_workflow_keeps_its_no_project_isolation() -> None:
    """The release workflow must not install the project environment.

    `uv run --no-project` consults neither the project nor its lockfile, so
    the release workflow is not exposed to lock drift. The guard must not
    sweep it into the project-installing set, and the isolation itself is
    unchanged.
    """
    text = workflow_text(RELEASE_WORKFLOW)
    assert text.count(RELEASE_ISOLATION) == RELEASE_ISOLATION_SITES
    assert project_environment_installs(RELEASE_WORKFLOW) == []
    assert RELEASE_WORKFLOW not in project_installing_workflows()


def test_the_release_docs_guard_runs_the_validator_without_uv() -> None:
    """The release-docs guard must run the validator under its own interpreter.

    `scripts/validate_release_docs.py` is pure standard library, so the `uv
    run` wrapper bought nothing but a network-capable subprocess on every CI
    job.
    """
    tree = ast.parse(RELEASE_DOCS_TEST.read_text(encoding="utf-8"))
    argvs = [
        node.args[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.args
    ]
    assert argvs, "the release-docs guard must still spawn the validator"
    for argv in argvs:
        elements = argv.elts if isinstance(argv, ast.List) else []
        assert not any(
            isinstance(element, ast.Constant) and element.value == "uv"
            for element in elements
        ), "the release-docs guard must not spawn uv"
        assert any(
            isinstance(element, ast.Attribute)
            and isinstance(element.value, ast.Name)
            and element.value.id == "sys"
            and element.attr == "executable"
            for element in elements
        ), "the release-docs guard must run the validator under sys.executable"
