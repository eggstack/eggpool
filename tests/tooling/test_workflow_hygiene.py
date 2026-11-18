"""Tests for the text-based GitHub Actions workflow hygiene guard."""

from __future__ import annotations

import pytest

from scripts.validate_workflow_hygiene import (
    WorkflowHygieneError,
    validate_workflow_text,
)

SHA = "a" * 40
VALID_WORKFLOW = f"""name: Example
on:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@{SHA} # v4.4.0
"""


def test_valid_fixture_accepts_sha_pinned_action() -> None:
    result = validate_workflow_text(VALID_WORKFLOW, "valid.yml")
    assert result == {"workflow": "valid.yml", "actions": 1, "status": "pass"}


def test_tag_reference_is_rejected_with_file_and_line() -> None:
    tagged = VALID_WORKFLOW.replace(f"actions/checkout@{SHA}", "actions/checkout@v4")
    with pytest.raises(WorkflowHygieneError) as error:
        validate_workflow_text(tagged, "tagged.yml")
    assert "tagged.yml:12:" in str(error.value)
    assert "actions/checkout@v4" in str(error.value)


def test_missing_top_level_permissions_is_rejected_with_file_and_line() -> None:
    missing = VALID_WORKFLOW.replace("permissions:\n  contents: read\n\n", "")
    with pytest.raises(WorkflowHygieneError) as error:
        validate_workflow_text(missing, "missing-permissions.yml")
    assert "missing-permissions.yml:1:" in str(error.value)
    assert "missing top-level permissions" in str(error.value)


def test_non_release_job_write_permission_is_rejected_with_file_and_line() -> None:
    writable = VALID_WORKFLOW.replace(
        "    runs-on: ubuntu-latest\n",
        "    runs-on: ubuntu-latest\n    permissions:\n      contents: write\n",
    )
    with pytest.raises(WorkflowHygieneError) as error:
        validate_workflow_text(writable, "writable.yml")
    assert "writable.yml:12:" in str(error.value)
    assert "job-level write permissions" in str(error.value)


def test_non_release_job_write_permission_is_rejected_with_alternate_indentation() -> (
    None
):
    writable = VALID_WORKFLOW.replace(
        "  check:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: ",
        "  check:\n"
        "      runs-on: ubuntu-latest\n"
        "      permissions:\n"
        "        contents: write\n"
        "      steps:\n"
        "        - uses: ",
    )
    with pytest.raises(WorkflowHygieneError) as error:
        validate_workflow_text(writable, "writable-indented.yml")
    assert "writable-indented.yml:12:" in str(error.value)
    assert "job-level write permissions" in str(error.value)


def test_non_release_job_write_permission_hidden_by_yaml_alias_is_rejected() -> None:
    aliased = VALID_WORKFLOW.replace(
        "permissions:\n  contents: read\n\njobs:",
        "permissions:\n  contents: read\n\nenv:\n  elevated: &elevated write\n\njobs:",
    ).replace(
        "    runs-on: ubuntu-latest\n    steps:",
        "    runs-on: ubuntu-latest\n"
        "    permissions:\n"
        "      contents: *elevated\n"
        "    steps:",
    )
    with pytest.raises(WorkflowHygieneError) as error:
        validate_workflow_text(aliased, "aliased-permissions.yml")
    assert "aliased-permissions.yml:" in str(error.value)
    assert "YAML aliases" in str(error.value)


def test_non_release_job_merge_key_is_rejected() -> None:
    merged = VALID_WORKFLOW.replace(
        "    runs-on: ubuntu-latest\n",
        "    runs-on: ubuntu-latest\n"
        "    <<:\n"
        "      permissions:\n"
        "        contents: write\n",
    )
    with pytest.raises(WorkflowHygieneError) as error:
        validate_workflow_text(merged, "merged-permissions.yml")
    assert "merged-permissions.yml:" in str(error.value)
    assert "merge keys" in str(error.value)


def test_non_release_inline_job_mapping_is_rejected() -> None:
    flow_mapping = VALID_WORKFLOW.replace(
        f"  check:\n    runs-on: ubuntu-latest\n    steps:\n"
        f"      - uses: actions/checkout@{SHA} # v4.4.0\n",
        "  check: {runs-on: ubuntu-latest, permissions: "
        "{contents: write}, steps: []}\n",
    )
    with pytest.raises(WorkflowHygieneError) as error:
        validate_workflow_text(flow_mapping, "inline-job.yml")
    assert "inline-job.yml:" in str(error.value)
    assert "inline job mappings" in str(error.value)


def test_non_release_top_level_permissions_must_be_exactly_contents_read() -> None:
    broader = VALID_WORKFLOW.replace(
        "permissions:\n  contents: read\n",
        "permissions:\n  contents: read\n  actions: read\n",
    )
    with pytest.raises(WorkflowHygieneError, match="exactly top-level contents: read"):
        validate_workflow_text(broader, "broader.yml")


def test_local_reusable_workflow_reference_is_accepted() -> None:
    local = VALID_WORKFLOW.replace(
        "  check:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: ",
        "  check:\n"
        "    uses: ./.github/workflows/reusable.yml\n"
        "    steps:\n"
        "      - uses: ",
    )
    assert validate_workflow_text(local, "local.yml")["actions"] == 2


def test_release_workflow_may_keep_job_level_write_permission() -> None:
    release = VALID_WORKFLOW.replace(
        "    runs-on: ubuntu-latest\n",
        "    runs-on: ubuntu-latest\n    permissions:\n      id-token: write\n",
    )
    assert validate_workflow_text(release, "release.yml")["status"] == "pass"
