"""Contract tests for the on-prem admin/member browser qualification gate.

These tests never start a server or a browser. They pin the ownership split
between the frozen Python oracle (historical pages) and the Rust-owned approved
reference (on-prem pages), the fixture's secret-leak sentinels, and the bounded
defect classes the gate is allowed to fix.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from scripts.dashboard_parity import on_prem
from scripts.dashboard_parity.on_prem import (
    _DOM_FACTS_TEMPLATE,
    ACCOUNT_SECRET,
    ADMIN_ONLY_PAGES,
    ADMIN_PAGES,
    ADMIN_PASSWORD,
    MEMBER_PAGES,
    MEMBER_PASSWORD,
    MEMBER_TEMP_PASSWORD,
    ON_PREM_REFERENCE,
    ON_PREM_SCHEMA,
    ON_PREM_THEMES,
    ON_PREM_VIEWPORTS,
    SECRET_MARKERS,
    STATIC_HEADER_SECRET,
    QualificationError,
    _assert_no_defects,
    _canonical,
    _dom_facts_expression,
    _reconcile_reference,
    _stable_rows,
    _write_on_prem_config,
    qualify_on_prem_browser,
)

_CLEAN_FACTS = {
    "duplicateIds": [],
    "horizontalOverflow": False,
    "tables": [],
    "forms": [],
    "secretMarkers": {},
    "viewportMeta": True,
}


def test_on_prem_module_owns_the_managed_browser_gate() -> None:
    assert qualify_on_prem_browser.__module__ == "scripts.dashboard_parity.on_prem"


def test_managed_config_plants_sentinel_secrets_without_a_server_key(
    tmp_path: Path,
) -> None:
    config = _write_on_prem_config(tmp_path, 18571)
    text = config.read_text(encoding="utf-8")
    for marker in (ACCOUNT_SECRET, STATIC_HEADER_SECRET):
        assert marker in text, f"fixture must plant {marker} for the leak guard"
    # The passwords are typed by the administrator rather than configured, but
    # they are still values that must never reach a rendered page.
    for marker in (ADMIN_PASSWORD, MEMBER_TEMP_PASSWORD, MEMBER_PASSWORD):
        assert marker not in text
    # The managed instance has no server API key; provider credentials are the
    # sentinels the leak guard is meant to catch.
    server_section = text.split("[database]")[0]
    assert "[server]" in server_section
    assert "api_key" not in server_section
    assert "api_key_env" not in server_section


def test_dom_facts_probe_reads_labels_tables_and_secret_markers() -> None:
    probe = _dom_facts_expression()
    for needle in (
        "duplicateIds",
        "horizontalOverflow",
        "table-scroll",
        "unlabelled",
        "viewportMeta",
        "__SECRET_MARKERS__",
    ):
        assert needle in _DOM_FACTS_TEMPLATE, needle
    # The placeholder is substituted at call time with the live markers.
    assert "__SECRET_MARKERS__" not in probe
    for marker in SECRET_MARKERS:
        assert marker in probe, marker


def test_defect_guard_rejects_each_bounded_class() -> None:
    defects = {
        "duplicateIds": [["user-card"]],
        "horizontalOverflow": True,
        "tables": [{"wrapped": False, "width": 1600}],
        "forms": [{"action": "/login", "unlabelled": ["input:username"]}],
        "secretMarkers": {SECRET_MARKERS[0]: True},
        "viewportMeta": False,
    }
    for field, value in defects.items():
        facts = dict(_CLEAN_FACTS)
        facts[field] = value
        with pytest.raises(QualificationError):
            _assert_no_defects("page.admin./users.desktop", facts, "admin")


def test_defect_guard_accepts_a_wrapped_scrolling_table() -> None:
    facts = dict(_CLEAN_FACTS)
    facts["tables"] = [{"wrapped": True, "width": 1600}]
    _assert_no_defects("page.admin./users.mobile", facts, "admin")


def test_stable_rows_drops_per_run_identifiers() -> None:
    rows = [{"username": "member", "id": "f0652a", "must_change": True}]
    assert _stable_rows(rows) == [{"username": "member", "must_change": True}]
    # The caller still needs the identifier to address the row's forms.
    assert rows[0]["id"] == "f0652a"


def test_canonical_is_stable_and_key_ordered() -> None:
    assert _canonical({"b": 1, "a": [2, {"d": 4, "c": 3}]}) == (
        '{"a":[2,{"c":3,"d":4}],"b":1}'
    )


def test_reconcile_rejects_drift_against_the_approved_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference = tmp_path / "reference.json"
    reference.write_text(
        json.dumps({"schema": ON_PREM_SCHEMA, "observations": {"a": "1"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(on_prem, "ON_PREM_REFERENCE", reference)
    with pytest.raises(QualificationError) as drift:
        _reconcile_reference({"a": "2"}, [], False)
    assert "a: 1 -> 2" in str(drift.value)
    assert _reconcile_reference({"a": "1"}, [], False)["result"] == "passed"
    with pytest.raises(QualificationError) as missing:
        _reconcile_reference({}, [], False)
    assert "absent from this run" in str(missing.value)
    with pytest.raises(QualificationError) as unknown:
        _reconcile_reference({"a": "1", "z": "9"}, [], False)
    assert "not in the approved reference" in str(unknown.value)


def test_reconcile_rejects_a_schema_change_in_the_approved_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference = tmp_path / "reference.json"
    reference.write_text(
        json.dumps({"schema": 0, "observations": {}}), encoding="utf-8"
    )
    monkeypatch.setattr(on_prem, "ON_PREM_REFERENCE", reference)
    with pytest.raises(QualificationError) as schema:
        _reconcile_reference({}, [], False)
    assert "schema" in str(schema.value)


def test_approved_reference_is_frozen_and_covers_the_required_surface() -> None:
    assert ON_PREM_REFERENCE.is_file(), "run the gate with --write-on-prem-reference"
    approved = json.loads(ON_PREM_REFERENCE.read_text(encoding="utf-8"))
    assert approved["schema"] == ON_PREM_SCHEMA
    observations: dict[str, str] = approved["observations"]
    for role, pages in (("admin", ADMIN_PAGES), ("member", MEMBER_PAGES)):
        for page in pages:
            for name, _width, _height, _scale in ON_PREM_VIEWPORTS:
                assert f"page.{role}.{page}.{name}" in observations
    for page in ADMIN_ONLY_PAGES:
        assert f"member_denied.{page}" in observations
        assert f"member_denied.{page}.message" in observations
    for page in ("/users", "/profiles", "/admin/audit"):
        assert f"anonymous.{page}" in observations
        assert f"after_logout.{page}" in observations
    # A personal agent credential must never open the dashboard.
    for page in ("/users", "/my-account"):
        assert f"agent_token.{page}" in observations
    for theme in ON_PREM_THEMES:
        key = f"page.admin./users.mobile.{theme}"
        assert key in observations
    for key in (
        "admin.sign_in",
        "admin.no_js_login",
        "admin.profile_created",
        "admin.member_created",
        "admin.member_row",
        "admin.member_suspended",
        "admin.member_reactivated",
        "admin.token_reveal",
        "admin.token_revealed_once",
        "admin.token_absent_on_return",
        "admin.override_saved",
        "admin.budget_saved",
        "admin.budget_labels",
        "admin.profile_conflict",
        "member.forced_password_landing",
        "member.password_changed",
        "member.budget_labelling",
        "restart.session_survives_restart",
        "restart.profiles_survive_restart",
        "restart.member_row",
    ):
        assert key in observations, key
    for value in observations.values():
        assert "\n" not in value
        for marker in SECRET_MARKERS:
            assert marker not in value
