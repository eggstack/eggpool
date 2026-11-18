#!/usr/bin/env python3
"""Check workflow action pins and least-privilege permissions without YAML parsing."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github/workflows"
RELEASE_WORKFLOWS = frozenset(
    {
        "release.yml",
        "qualification-public-release.yml",
        "qualification-release-artifacts.yml",
    }
)
ACTION_REF = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-fA-F]{40}$")
USES_FIELD = re.compile(
    r"^\s*(?:-\s*)?uses:\s*(?P<reference>\"[^\"]*\"|'[^']*'|[^#]*?)\s*(?:#.*)?$"
)
TOP_LEVEL_PERMISSIONS = re.compile(r"permissions:[ \t]*(?:#.*)?$")
TOP_LEVEL_PERMISSION_ENTRY = re.compile(
    r"^  ([A-Za-z0-9_-]+):[ \t]*([^#\s]+)[ \t]*(?:#.*)?$"
)
JOB_PERMISSIONS = re.compile(r"^permissions:[ \t]*(.*?)[ \t]*(?:#.*)?$")
WRITE_SCOPE = re.compile(r"(?<![A-Za-z0-9_-])(?:write|write-all)(?![A-Za-z0-9_-])")
YAML_ALIAS_OR_ANCHOR = re.compile(r"(?<![A-Za-z0-9_-])[&*][A-Za-z0-9_-]+")
YAML_FLOW_COLLECTION = re.compile(r"[\{\[]")
YAML_MERGE_KEY = re.compile(r"^<<[ \t]*:")


class WorkflowHygieneError(ValueError):
    """A workflow violates the repository-wide workflow hygiene contract."""


def _extract_reference(value: str) -> str:
    reference = value.strip()
    if len(reference) >= 2 and reference[0] in "\"'" and reference[-1] == reference[0]:
        return reference[1:-1]
    return reference


def _permission_header(
    lines: list[str], workflow: str
) -> tuple[int, list[tuple[str, str]]]:
    headers = [
        index
        for index, line in enumerate(lines)
        if TOP_LEVEL_PERMISSIONS.fullmatch(line)
    ]
    if not headers:
        raise WorkflowHygieneError(f"{workflow}:1: missing top-level permissions block")
    if len(headers) > 1:
        raise WorkflowHygieneError(
            f"{workflow}:{headers[1] + 1}: duplicate top-level permissions block"
        )

    header = headers[0]
    entries: list[tuple[str, str]] = []
    for index in range(header + 1, len(lines)):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith(" "):
            break
        match = TOP_LEVEL_PERMISSION_ENTRY.fullmatch(line)
        if match:
            entries.append((match.group(1), match.group(2)))
        else:
            entries.append((f"<invalid:{index + 1}>", ""))
    return header + 1, entries


def _job_permission_alias_line(lines: list[str]) -> int | None:
    """Reject YAML indirection or flow maps that can hide job permissions."""
    in_jobs = False
    job_indent: int | None = None
    property_indent: int | None = None
    permissions_indent: int | None = None
    for index, line in enumerate(lines):
        content = line.split("#", 1)[0]
        stripped = content.lstrip(" ")
        if not stripped:
            continue
        indent = len(content) - len(stripped)
        if indent == 0:
            jobs_header = re.fullmatch(r"jobs:[ \t]*(.*)", content)
            in_jobs = jobs_header is not None
            job_indent = None
            property_indent = None
            permissions_indent = None
            if jobs_header is not None:
                value = jobs_header.group(1).strip()
                if YAML_ALIAS_OR_ANCHOR.search(value) or YAML_FLOW_COLLECTION.search(
                    value
                ):
                    return index + 1
            continue
        if not in_jobs:
            continue

        if job_indent is None:
            # A job entry must be a block-style mapping, not an alias or a
            # flow-style map whose permissions cannot be inspected safely.
            job_indent = indent
            key, separator, value = stripped.partition(":")
            if (
                not separator
                or YAML_ALIAS_OR_ANCHOR.search(key)
                or YAML_ALIAS_OR_ANCHOR.search(value)
                or YAML_FLOW_COLLECTION.search(value)
            ):
                return index + 1
            continue
        if indent == job_indent:
            # A sibling job starts; its properties may use another valid indent.
            property_indent = None
            permissions_indent = None
            key, separator, value = stripped.partition(":")
            if (
                not separator
                or YAML_ALIAS_OR_ANCHOR.search(key)
                or YAML_ALIAS_OR_ANCHOR.search(value)
                or YAML_FLOW_COLLECTION.search(value)
            ):
                return index + 1
            continue
        if indent < job_indent:
            in_jobs = False
            property_indent = None
            permissions_indent = None
            continue
        if property_indent is None:
            property_indent = indent

        if indent == property_indent:
            if YAML_MERGE_KEY.match(stripped):
                return index + 1
            key, _, _ = stripped.partition(":")
            if YAML_ALIAS_OR_ANCHOR.search(key):
                return index + 1
            header = JOB_PERMISSIONS.fullmatch(stripped)
            if header is None:
                permissions_indent = None
                continue
            permissions_indent = indent
            value = header.group(1).split("#", 1)[0]
            if YAML_ALIAS_OR_ANCHOR.search(value):
                return index + 1
            continue

        if permissions_indent is not None and indent > permissions_indent:
            permission_line = stripped.split("#", 1)[0]
            if YAML_MERGE_KEY.match(permission_line) or YAML_ALIAS_OR_ANCHOR.search(
                permission_line
            ):
                return index + 1
    return None


def _job_write_permission_line(lines: list[str]) -> int | None:
    in_jobs = False
    job_indent: int | None = None
    property_indent: int | None = None
    permissions_indent: int | None = None
    for index, line in enumerate(lines):
        stripped = line.lstrip(" ")
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(stripped)
        if indent == 0:
            in_jobs = re.fullmatch(r"jobs:[ \t]*(?:#.*)?", line) is not None
            job_indent = None
            property_indent = None
            permissions_indent = None
            continue
        if not in_jobs:
            continue

        if job_indent is None:
            # The first indented mapping key under `jobs:` begins the job map.
            job_indent = indent
            continue
        if indent == job_indent:
            # A sibling job starts; its properties may use another valid indent.
            property_indent = None
            permissions_indent = None
            continue
        if indent < job_indent:
            in_jobs = False
            property_indent = None
            permissions_indent = None
            continue
        if property_indent is None:
            property_indent = indent

        if indent == property_indent:
            header = JOB_PERMISSIONS.fullmatch(stripped)
            if header is None:
                permissions_indent = None
                continue
            permissions_indent = indent
            if WRITE_SCOPE.search(header.group(1).split("#", 1)[0]):
                return index + 1
            continue

        if (
            permissions_indent is not None
            and indent > permissions_indent
            and WRITE_SCOPE.search(stripped.split("#", 1)[0])
        ):
            return index + 1
    return None


def validate_workflow_text(
    text: str, workflow: str = "<workflow>"
) -> dict[str, object]:
    """Validate one workflow's action refs and permissions, returning a summary."""
    lines = text.splitlines()
    action_count = 0
    for line_number, line in enumerate(lines, start=1):
        match = USES_FIELD.fullmatch(line)
        if match is None:
            continue
        action_count += 1
        reference = _extract_reference(match.group("reference"))
        local_reference = (
            reference.startswith("./") and len(reference) > 2 and "@" not in reference
        )
        if local_reference or ACTION_REF.fullmatch(reference):
            continue
        raise WorkflowHygieneError(
            f"{workflow}:{line_number}: action reference must be OWNER/REPO@40-hex-SHA "
            f"or a local ./ path: {reference!r}"
        )

    permissions_line, permissions = _permission_header(lines, workflow)
    if Path(workflow).name not in RELEASE_WORKFLOWS:
        if permissions != [("contents", "read")]:
            raise WorkflowHygieneError(
                f"{workflow}:{permissions_line}: non-release workflows must declare "
                "exactly top-level contents: read permissions"
            )
        alias_line = _job_permission_alias_line(lines)
        if alias_line is not None:
            raise WorkflowHygieneError(
                f"{workflow}:{alias_line}: non-release job permissions "
                "must be explicit; "
                "YAML aliases, anchors, merge keys, and inline job mappings that can "
                "obscure permissions are unsupported"
            )
        write_line = _job_write_permission_line(lines)
        if write_line is not None:
            raise WorkflowHygieneError(
                f"{workflow}:{write_line}: non-release workflows must not grant "
                "job-level write permissions"
            )

    return {"workflow": workflow, "actions": action_count, "status": "pass"}


def _repository_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def main() -> int:
    workflows = sorted(WORKFLOW_DIR.glob("*.yml"))
    if not workflows:
        print(
            f"workflow hygiene validation failed: no .yml files in {WORKFLOW_DIR}",
            file=sys.stderr,
        )
        return 1

    failed = False
    for path in workflows:
        name = _repository_relative(path)
        try:
            summary = validate_workflow_text(path.read_text(encoding="utf-8"), name)
        except (OSError, WorkflowHygieneError) as error:
            print(f"workflow hygiene validation failed: {error}", file=sys.stderr)
            failed = True
        else:
            print(f"{summary['workflow']}: pass (actions={summary['actions']})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
