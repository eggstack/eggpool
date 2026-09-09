"""Validation of the frozen M10 qualification contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
MANIFEST_PATH = ROOT / "migration-rs/fixtures/qualification/m10-q001-manifest.json"
O001_PATH = ROOT / "migration-rs/fixtures/operations/o001-fixture-matrix.json"


def load_manifest() -> dict[str, Any]:
    with MANIFEST_PATH.open(encoding="utf-8") as handle:
        value = json.load(handle)
    assert isinstance(value, dict)
    return value


def test_manifest_has_unique_cells_and_complete_required_shape() -> None:
    manifest = load_manifest()
    required = set(manifest["cell_schema"]["required_fields"])
    observation_classes = set(manifest["enums"]["observation_class"])
    environment_classes = set(manifest["enums"]["environment_class"])
    owners = set(manifest["enums"]["owner_plan"])
    closure_statuses = set(manifest["enums"]["closure_status"])

    cells = manifest["cells"]
    ids = [cell["id"] for cell in cells]
    assert len(ids) == len(set(ids))

    for cell in cells:
        assert required <= cell.keys(), cell["id"]
        assert cell["observation_class"] in observation_classes
        assert cell["environment_class"] in environment_classes
        assert cell["owner_plan"] is None or cell["owner_plan"] in owners
        assert cell["normalization_rule"] in manifest["normalization_rules"]
        assert cell["closure_status"] in closure_statuses
        assert isinstance(cell["existing_evidence"], list)
        assert isinstance(cell["risk_flags"], list)
        assert cell["mandatory"] is True
        assert cell["existing_evidence"] or cell["owner_plan"] is not None
        for evidence_path in cell["existing_evidence"]:
            assert (ROOT / evidence_path).exists(), (cell["id"], evidence_path)


def test_manifest_covers_every_frozen_o001_command_path() -> None:
    manifest = load_manifest()
    with O001_PATH.open(encoding="utf-8") as handle:
        o001 = json.load(handle)

    expected = {row["path"] for row in o001["commands"]}
    actual = {
        cell["command_path"] for cell in manifest["cells"] if "command_path" in cell
    }
    assert len(expected) == 63
    assert actual == expected


def test_manifest_covers_all_public_inference_surfaces_and_modes() -> None:
    manifest = load_manifest()
    observed = {
        (cell["inference_surface"], cell["traffic_mode"])
        for cell in manifest["cells"]
        if "inference_surface" in cell
    }
    assert observed == {
        (surface, mode)
        for surface in ("chat_completions", "responses", "messages")
        for mode in ("finite", "streaming")
    }


def test_manifest_has_all_environment_categories_and_future_owners() -> None:
    manifest = load_manifest()
    cells = manifest["cells"]
    subsystems = {cell["subsystem"] for cell in cells}
    owners = {cell["owner_plan"] for cell in cells}
    assert {"database", "dashboard", "platform", "provider", "stability"} <= subsystems
    assert {"Q002", "Q003", "Q004", "Q005", "Q006", "Q007", "Q008", "Q009"} <= owners
    assert {
        "normal-ci-smoke",
        "manual-local-deterministic",
        "workflow_dispatch",
        "physical-host-only",
        "credentialed-live-provider-only",
    } == {entry["execution_class"] for entry in manifest["ci_policy"]}


def test_manifest_freezes_explicit_target_support_decisions() -> None:
    manifest = load_manifest()
    targets = {target["id"]: target for target in manifest["targets"]}
    assert targets["linux-x86_64"]["classification"] == "supported"
    assert targets["linux-aarch64"]["classification"] == "supported"
    assert targets["macos-arm64"]["classification"] == "supported-development"
    assert targets["other-unix"]["classification"] == "not-qualified"
    assert targets["windows"]["classification"] == "unsupported"
    assert targets["windows"]["required_environment_classes"] == []


def test_manifest_contains_no_credential_bearing_fixture_fields() -> None:
    manifest = load_manifest()
    forbidden_fragments = (
        "credential_value",
        "api_key_value",
        "token_value",
        "password_value",
        "authorization_value",
        "request_body",
        "response_body",
        "raw_body",
    )

    def check(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                assert not any(
                    fragment in key.lower() for fragment in forbidden_fragments
                ), key
                check(nested)
        elif isinstance(value, list):
            for nested in value:
                check(nested)

    check(manifest)
    assert manifest["evidence_record_schema"]["allowlisted_environment_variables"]
    assert "credentials" not in manifest["evidence_record_schema"]["required_fields"]
    assert "credentials" not in manifest["evidence_record_schema"]["optional_fields"]
