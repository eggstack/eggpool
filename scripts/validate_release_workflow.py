#!/usr/bin/env python3
"""Validate the release workflow without a YAML runtime dependency."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ACTION_SHA = re.compile(r"^[0-9a-f]{40}$")
TARGETS = ("linux-x86_64", "linux-aarch64", "macos-arm64")
CONNECT_TARGETS = (
    "connect-linux-x86_64",
    "connect-linux-aarch64",
    "connect-macos-arm64",
    "connect-windows-x86_64",
)
CONNECT_JOBS = {
    "build-connect-linux-x86-64": (
        "connect-linux-x86_64",
        "ubuntu-",
        "x86_64-unknown-linux-gnu",
    ),
    "build-connect-linux-aarch64": (
        "connect-linux-aarch64",
        "ubuntu-",
        "aarch64-unknown-linux-gnu",
    ),
    "build-connect-macos-arm64": (
        "connect-macos-arm64",
        "macos-",
        "aarch64-apple-darwin",
    ),
    "build-connect-windows-x86-64": (
        "connect-windows-x86_64",
        "windows-",
        "x86_64-pc-windows-msvc",
    ),
}
FORBIDDEN_SECRET_MARKERS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "EGGPOOL_API_KEY",
    "PROXY_PASSWORD",
    "PYPI_TOKEN",
    "TWINE_PASSWORD",
)


class WorkflowValidationError(ValueError):
    """A release workflow violates the release workflow supply-chain contract."""


def job_blocks(text: str) -> dict[str, str]:
    lines = text.splitlines(keepends=True)
    try:
        jobs_index = next(
            index
            for index, line in enumerate(lines)
            if re.fullmatch(r"jobs:\s*\n?", line)
        )
    except StopIteration as error:
        raise WorkflowValidationError("workflow has no jobs mapping") from error

    starts = [
        (index, match.group(1))
        for index, line in enumerate(lines[jobs_index + 1 :], jobs_index + 1)
        if (match := re.fullmatch(r"  ([a-z][a-z0-9-]*):\s*\n?", line))
    ]
    if not starts:
        raise WorkflowValidationError("workflow has no jobs")
    blocks: dict[str, str] = {}
    for position, (start, name) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        blocks[name] = "".join(lines[start:end])
    return blocks


def without_helper_blocks(text: str, blocks: dict[str, str]) -> str:
    """Return workflow text with helper `build-connect-*` jobs removed.

    The Windows helper is a desktop-only asset: the word "windows" may appear
    solely inside its own helper build job. Every other job (wheels, manifest,
    publish, verify) must stay free of Windows/universal artifact paths so a
    helper binary can never be mistaken for proxy support.
    """
    remaining = text
    for job in CONNECT_JOBS:
        block = blocks.get(job)
        if block is not None and block in remaining:
            remaining = remaining.replace(block, "", 1)
    # Aggregate `needs` entries reference the helper jobs by name; those
    # references are part of the helper contract, not proxy support.
    remaining = re.sub(r"(?m)^\s+- build-connect-[a-z0-9-]+\s*\n", "", remaining)
    return remaining


def _require(text: str, pattern: str, message: str) -> None:
    if re.search(pattern, text, re.MULTILINE) is None:
        raise WorkflowValidationError(message)


def _require_in_block(
    blocks: dict[str, str], job: str, pattern: str, message: str
) -> None:
    block = blocks.get(job)
    if block is None:
        raise WorkflowValidationError(f"required job is missing: {job}")
    _require(block, pattern, message)


def _validate_action_pins(text: str) -> None:
    refs = re.findall(r"^\s*-?\s*uses:\s*([^\s#]+)\s*$", text, re.MULTILINE)
    if not refs:
        raise WorkflowValidationError("workflow has no actions")
    for reference in refs:
        if "@" not in reference:
            raise WorkflowValidationError(f"action is not pinned: {reference}")
        _, revision = reference.rsplit("@", 1)
        if not ACTION_SHA.fullmatch(revision):
            raise WorkflowValidationError(
                f"action is not pinned to an immutable SHA: {reference}"
            )


def validate_workflow_text(text: str) -> dict[str, object]:
    """Return a bounded summary when the workflow satisfies release workflow."""
    if "\t" in text:
        raise WorkflowValidationError("workflow must not use tab indentation")
    _require(text, r"^on:\s*$", "workflow trigger mapping is missing")
    _require(text, r"^  push:\s*$", "tag push trigger is missing")
    _require(text, r"^    tags:\s*$", "tag trigger filter is missing")
    _require(
        text,
        r"^      - ['\"]v\*\.\*\.\*['\"]\s*$",
        "release tag filter is too broad or missing",
    )
    if re.search(r"^\s+pull_request:\s*$", text, re.MULTILINE):
        raise WorkflowValidationError("pull requests must not trigger release jobs")
    _require(text, r"^  workflow_dispatch:\s*$", "manual rehearsal trigger is missing")
    _require(text, r"^      destination:\s*$", "manual destination input is missing")
    _require(text, r"^          - validate\s*$", "manual validate mode is missing")
    _require(text, r"^          - testpypi\s*$", "manual TestPyPI mode is missing")
    _require(text, r"^          - pypi-resume\s*$", "PyPI recovery mode is missing")
    _require(
        text, r"^      source_run_id:\s*$", "PyPI recovery source input is missing"
    )

    _require(text, r"^permissions:\s*$", "read-only default permissions are missing")
    _require(
        text, r"^  contents: read\s*$", "default contents permission must be read-only"
    )
    if re.search(
        r"^permissions:\s*\n(?:  .*\n)*?  id-token:\s*write", text, re.MULTILINE
    ):
        raise WorkflowValidationError("OIDC permission must be job-scoped")
    _validate_action_pins(text)
    for marker in FORBIDDEN_SECRET_MARKERS:
        if marker in text:
            raise WorkflowValidationError(
                f"provider or long-lived package secret is referenced: {marker}"
            )
    if re.search(r"\bsecrets\.[A-Za-z0-9_]+", text):
        raise WorkflowValidationError(
            "release jobs must not consume repository secrets"
        )
    if re.search(r"(?im)^.*\bsdist\b.*$", text):
        raise WorkflowValidationError(
            "the normal release path must not build or publish an sdist"
        )

    blocks = job_blocks(text)
    required_jobs = {
        "validate-release",
        "build-wheel-linux-x86-64",
        "build-wheel-linux-aarch64",
        "build-wheel-macos-arm64",
        "build-connect-linux-x86-64",
        "build-connect-linux-aarch64",
        "build-connect-macos-arm64",
        "build-connect-windows-x86-64",
        "aggregate-release-manifest",
        "publish-testpypi",
        "publish-pypi",
        "publish-github-release",
        "verify-production-release",
    }
    missing = sorted(required_jobs - blocks.keys())
    if missing:
        raise WorkflowValidationError(
            f"required jobs are missing: {', '.join(missing)}"
        )

    for job in required_jobs - {
        "publish-testpypi",
        "publish-pypi",
        "publish-github-release",
        "verify-production-release",
    }:
        block = blocks[job]
        if re.search(r"(?m)^    permissions:\s*$", block) is None:
            raise WorkflowValidationError(f"job permissions are not explicit: {job}")
        if "id-token: write" in block or "contents: write" in block:
            raise WorkflowValidationError(
                f"build/validation job is over-privileged: {job}"
            )

    _require_in_block(
        blocks,
        "validate-release",
        r"github\.event_name\s*==\s*'push'",
        "tag validation must distinguish tag pushes",
    )
    _require_in_block(
        blocks,
        "validate-release",
        r"validate_release_identity\.py",
        "release identity validator is missing",
    )
    _require_in_block(
        blocks,
        "validate-release",
        r"validate_release_docs\.py",
        "public metadata/docs guard is missing",
    )
    _require_in_block(
        blocks,
        "validate-release",
        r"validate_runtime_package_boundary\.py",
        "runtime current-package boundary guard is missing",
    )
    for job, target in zip(
        (
            "build-wheel-linux-x86-64",
            "build-wheel-linux-aarch64",
            "build-wheel-macos-arm64",
        ),
        TARGETS,
        strict=True,
    ):
        _require_in_block(
            blocks,
            job,
            rf"--target-class\s+{re.escape(target)}",
            f"target build is missing: {target}",
        )
        _require_in_block(
            blocks,
            job,
            r"needs:\s+validate-release",
            f"target build is not gated: {target}",
        )
        _require_in_block(
            blocks, job, r"maturin==1\.14\.1", f"Maturin pin is missing: {target}"
        )
        _require_in_block(
            blocks,
            job,
            r"build_release_artifacts\.py",
            f"release artifacts builder is missing: {target}",
        )
        _require_in_block(
            blocks,
            job,
            r"qualify_release_wheel\.py",
            f"wheel smoke is missing: {target}",
        )
        _require_in_block(
            blocks,
            job,
            r"upload-artifact",
            f"target artifact upload is missing: {target}",
        )
    for target in TARGETS:
        if text.count(target) < 2:
            raise WorkflowValidationError(f"target matrix is incomplete: {target}")
    proxy_text = without_helper_blocks(text, blocks)
    if "windows" in proxy_text.lower():
        raise WorkflowValidationError(
            "Windows appears outside the desktop-helper build job"
        )
    if "py3-none-any" in text:
        raise WorkflowValidationError(
            "unsupported/universal target appears in release workflow"
        )
    for job, (target, runner_prefix, rust_target) in CONNECT_JOBS.items():
        _require_in_block(
            blocks,
            job,
            r"needs:\s+validate-release",
            f"helper build is not gated: {target}",
        )
        _require_in_block(
            blocks,
            job,
            rf"--target-class\s+{re.escape(target)}",
            f"helper build is missing: {target}",
        )
        _require_in_block(
            blocks,
            job,
            r"build_connect_artifacts\.py",
            f"helper builder is missing: {target}",
        )
        _require_in_block(
            blocks,
            job,
            r"upload-artifact",
            f"helper artifact upload is missing: {target}",
        )
        _require_in_block(
            blocks,
            job,
            rf"runs-on:\s+{re.escape(runner_prefix)}",
            f"helper runner is wrong for {target}",
        )
        _require_in_block(
            blocks,
            job,
            rf"targets:\s+{re.escape(rust_target)}",
            f"helper toolchain target is missing: {target}",
        )
        block = blocks[job]
        if re.search(r"maturin|build_release_artifacts\.py|\.whl", block):
            raise WorkflowValidationError(f"helper job must not build wheels: {job}")
        if re.search(r"(?m)^\s*uses:\s*pypa/gh-action-pypi-publish@", block):
            raise WorkflowValidationError(f"helper job must not publish to PyPI: {job}")
    for target in CONNECT_TARGETS:
        if text.count(target) < 2:
            raise WorkflowValidationError(
                f"helper target matrix is incomplete: {target}"
            )

    aggregate = blocks["aggregate-release-manifest"]
    _require(
        aggregate,
        r"needs:\s*\n(?:\s+- .*\n){3}",
        "manifest job must depend on every target build",
    )
    for helper_job in CONNECT_JOBS:
        _require(
            aggregate,
            rf"^\s+- {re.escape(helper_job)}\s*$",
            f"manifest job must depend on helper build: {helper_job}",
        )
    _require(
        aggregate,
        r"validate_release_artifacts\.py",
        "manifest hash validation is missing",
    )
    _require(
        aggregate,
        r"create_release_manifest\.py",
        "release manifest creation is missing",
    )
    _require(
        aggregate,
        r"build_connect_artifacts|connect-artifact-dir|dist/connect",
        "helper bundle aggregation is missing",
    )
    _require(aggregate, r"SHA256SUMS", "raw/wheel checksum sidecar is missing")
    _require(
        aggregate, r"upload-artifact", "validated release bundle upload is missing"
    )

    testpypi = blocks["publish-testpypi"]
    _require(
        testpypi,
        r"github\.event_name\s*==\s*'workflow_dispatch'",
        "TestPyPI must be manual-only",
    )
    _require(
        testpypi,
        r"inputs\.destination\s*==\s*'testpypi'",
        "TestPyPI destination gate is missing",
    )
    _require(
        testpypi,
        r"environment:\s*\n\s+name:\s+testpypi",
        "TestPyPI environment is missing",
    )
    _require(
        testpypi,
        r"repository-url:\s+https://test\.pypi\.org/legacy/",
        "TestPyPI URL is missing",
    )
    _require(
        testpypi, r"attestations:\s+true", "TestPyPI attestations must stay enabled"
    )
    _require(testpypi, r"pypa/gh-action-pypi-publish@", "PyPA publisher is missing")
    _require(
        testpypi,
        r"needs:\s+aggregate-release-manifest",
        "TestPyPI must consume validated artifacts",
    )

    pypi = blocks["publish-pypi"]
    _require(
        pypi,
        r"github\.event_name\s*==\s*'push'",
        "production PyPI must retain the tag-push path",
    )
    _require(
        pypi,
        r"github\.repository\s*==\s*'eggstack/eggpool'",
        "production PyPI repository is not fixed",
    )
    _require(
        pypi,
        r"github\.ref\s*==\s*format\("
        r"'refs/tags/v\{0\}', needs\.validate-release\.outputs\.version\)",
        "production version/tag gate is missing",
    )
    _require(pypi, r"id-token:\s+write", "production PyPI job lacks OIDC permission")
    _require(
        pypi,
        r"actions:\s+read",
        "PyPI recovery lacks scoped artifact-read permission",
    )
    _require(
        pypi,
        r"inputs\.destination\s*==\s*'pypi-resume'",
        "PyPI recovery destination gate is missing",
    )
    _require(
        pypi,
        r"inputs\.source_run_id\s*!=\s*''",
        "PyPI recovery source-run gate is missing",
    )
    _require(
        pypi,
        r"run-id:\s+\$\{\{ inputs\.source_run_id \}\}",
        "PyPI recovery must select an explicit prior run",
    )
    _require(
        pypi,
        r"github\.token",
        "PyPI recovery artifact download must use the workflow token",
    )
    _require(
        pypi,
        r"validate exact failed-run bundle provenance",
        "PyPI recovery provenance check is missing",
    )
    _require(
        pypi,
        r"environment:\s*\n\s+name:\s+pypi",
        "protected PyPI environment is missing",
    )
    if "repository-url:" in pypi or "password:" in pypi or "user:" in pypi:
        raise WorkflowValidationError(
            "production PyPI job must use the default Trusted Publishing "
            "destination without credentials"
        )
    _require(pypi, r"attestations:\s+true", "production attestations must stay enabled")
    _require(
        pypi,
        r"aggregate-release-manifest",
        "production PyPI must consume validated artifacts",
    )

    github_release = blocks["publish-github-release"]
    _require(
        github_release,
        r"contents:\s+write",
        "GitHub release job lacks scoped contents write permission",
    )
    if "id-token: write" in github_release:
        raise WorkflowValidationError(
            "GitHub release job must not receive OIDC permission"
        )
    _require(
        github_release,
        r"softprops/action-gh-release@",
        "GitHub release action is missing",
    )
    _require(
        github_release,
        r"overwrite_files:\s+false",
        "GitHub assets must not be overwritten",
    )
    _require(github_release, r"SHA256SUMS", "GitHub release must carry checksums")
    _require(
        github_release,
        r"connect",
        "GitHub release must attach helper assets",
    )
    _require(
        github_release,
        r"aggregate-release-manifest",
        "GitHub release must consume validated artifacts",
    )
    if re.search(r"github\.event_name\s*==\s*'workflow_dispatch'", github_release):
        raise WorkflowValidationError(
            "manual rehearsal must not publish a GitHub release"
        )

    verify = blocks["verify-production-release"]
    _require(
        verify,
        r"verify_published_release\.py",
        "post-publication verification is missing",
    )
    _require(
        verify,
        r"needs:\s*\n(?:\s+- .*\n){2}",
        "post-publication verification must wait for both publish jobs",
    )

    return {
        "status": "pass",
        "actions": len(re.findall(r"^\s*-?\s*uses:", text, re.MULTILINE)),
        "jobs": sorted(blocks),
        "targets": list(TARGETS),
        "connect_targets": list(CONNECT_TARGETS),
    }


def validate_workflow(path: Path) -> dict[str, object]:
    return validate_workflow_text(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", type=Path)
    args = parser.parse_args(argv)
    try:
        print(validate_workflow(args.workflow.resolve()))
    except (OSError, WorkflowValidationError) as error:
        print(f"release workflow workflow validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
