"""Focused Q012 dashboard fixture, semantic, and visual qualification tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
from typing import TYPE_CHECKING

import pytest

from scripts import qualification_dashboard as q

if TYPE_CHECKING:
    from pathlib import Path


def _shell(
    *,
    active: str = "/accounts",
    form: bool = True,
    body: str = "",
    title: str = "Accounts",
    heading: str = "Accounts",
) -> str:
    links = "".join(
        f'<a class="{"active" if path == active else ""}" href="{path}">{path}</a>'
        for path in q.NAV_PATHS
    )
    controls = '<form method="get"><select id="period" name="period"></select></form>'
    if not form:
        controls = '<form method="get"><input name="limit"></form>'
    return f"""<!doctype html>
<html>
<head><title>{title}</title>
<link rel="stylesheet" href="/static/dashboard.css"></head>
<body><nav><div id="topnav-menu">{links}</div></nav>
<main id="dashboard-content"><h2>{heading}</h2>{controls}{body}
<span id="dashboard-updated">ready</span></main></body></html>"""


def _account_table(rows: tuple[tuple[str, ...], ...]) -> str:
    headers = ("Account", "Provider", "Enabled", "Requests", "Cost")
    head = "".join(f"<th>{header}</th>" for header in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _accounts_projection(
    rows: tuple[tuple[str, ...], ...], *, form: bool = True
) -> q.HtmlProjection:
    return q.project_html(
        _shell(active="/accounts", form=form, body=_account_table(rows))
    )


def test_q012_fixture_is_versioned_bounded_and_secret_free(tmp_path: Path) -> None:
    fixture = tmp_path / "q012.sqlite3"
    inventory = q._build_fixture(fixture)
    assert inventory == {
        "path": "migration-rs/fixtures/dashboard/q012-populated.sql",
        "sha256": hashlib.sha256(q.Q012_FIXTURE.read_bytes()).hexdigest(),
        "providers": 2,
        "accounts": 3,
        "models": 3,
        "requests": 5,
        "attempts": 6,
        "events": 2,
        "pings": 3,
        "routing_decisions": 5,
        "secrets": "none",
    }
    connection = sqlite3.connect(fixture)
    try:
        for table, expected in (
            ("providers", 2),
            ("accounts", 3),
            ("requests", 5),
            ("request_attempts", 6),
            ("account_events", 2),
            ("provider_pings", 3),
            ("routing_decisions", 5),
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (
                expected,
            )
        assert connection.execute(
            "SELECT COUNT(*) FROM models WHERE model_id != '__deprecated__'"
        ).fetchone() == (3,)
        assert connection.execute("SELECT COUNT(*) FROM _migrations").fetchone()[0] > 0
    finally:
        connection.close()
    fixture_text = q.Q012_FIXTURE.read_text(encoding="utf-8")
    assert "sk-" not in fixture_text
    assert "api_key =" not in fixture_text
    assert len(fixture_text.encode()) < q.MAX_RESULT_BYTES


def test_q012_projection_extracts_cards_tables_status_and_controls() -> None:
    projection = q.project_html(
        _shell(
            body="""
<section class="card"><h3>Requests</h3><p class="metric">5</p></section>
<p class="status">Error: provider unavailable</p>
"""
            + _account_table((("alpha", "provider", "yes", "5", "$1.00"),))
        )
    )
    assert projection.active_nav == "/accounts"
    assert projection.cards == (("Requests", "5"),)
    assert projection.tables[0].headers[:2] == ("Account", "Provider")
    assert projection.tables[0].rows[0][0] == "alpha"
    assert projection.status_messages == ("Error: provider unavailable",)
    assert any(
        "select:period" in inputs
        for _method, _action, inputs in projection.form_signatures
    )


def test_q012_runner_plan_has_actual_artifacts_for_every_major_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = q.screenshot_plan(tmp_path)
    assert len(entries) == len(q.SCREENSHOT_ROUTES) * 2 == 28
    assert {entry["implementation"] for entry in entries} == {"python", "rust"}
    assert {entry["viewport"] for entry in entries} == {"desktop", "mobile"}
    assert {entry["theme"] for entry in entries} == {"default", "Catppuccin Latte"}
    assert all(entry["state"] == "populated" for entry in entries)
    assert {entry["route"] for entry in entries} == {
        route for route, _label in q.SCREENSHOT_ROUTES
    }

    def fake_capture(url: str, artifact: Path, width: int, height: int) -> None:
        del url
        artifact.parent.mkdir(parents=True, exist_ok=True)
        payload = bytearray(24)
        payload[:8] = b"\x89PNG\r\n\x1a\n"
        struct.pack_into(">II", payload, 16, width, height)
        artifact.write_bytes(payload)

    monkeypatch.setattr(q, "_capture_browser_screenshot", fake_capture)
    manifest = q.capture_screenshots(
        ports={"python": 31001, "rust": 31002}, output_dir=tmp_path
    )
    assert manifest["count"] == 28
    assert len(manifest["manifest_sha256"]) == 64
    assert all(
        (tmp_path / str(entry["artifact"])).is_file() for entry in manifest["entries"]
    )
    assert all(
        entry["dimensions"] == {"width": entry["width"], "height": entry["height"]}
        for entry in manifest["entries"]
    )
    assert all(
        entry["bytes"] > 0 and len(entry["sha256"]) == 64
        for entry in manifest["entries"]
    )
    assert len(json.dumps(manifest).encode()) < q.MAX_RESULT_BYTES // 4


@pytest.mark.parametrize(
    ("actual_body", "message"),
    [
        (
            '<section class="card"><h3>Requests</h3><p class="metric">6</p></section>',
            "card 'Requests'",
        ),
    ],
)
def test_q012_rejects_changed_metric_card(actual_body: str, message: str) -> None:
    expected = q.project_html(
        _shell(
            active="/",
            title="Overview",
            heading="Overview",
            body=(
                '<section class="card"><h3>Requests</h3>'
                '<p class="metric">5</p></section>'
            ),
        )
    )
    actual = q.project_html(
        _shell(active="/", title="Overview", heading="Overview", body=actual_body)
    )
    with pytest.raises(AssertionError, match=message):
        q.compare_dom_projection(expected, actual, "/")


@pytest.mark.parametrize(
    ("actual_rows", "message"),
    [
        (
            (("alpha", "provider", "yes", "5", "$1.00"),),
            "table projection",
        ),
        (
            (
                ("alpha", "provider", "yes", "6", "$1.00"),
                ("beta", "provider", "no", "0", "$0.00"),
            ),
            "table projection",
        ),
        (
            (
                ("beta", "provider", "no", "0", "$0.00"),
                ("alpha", "provider", "yes", "5", "$1.00"),
            ),
            "table projection",
        ),
    ],
)
def test_q012_rejects_missing_changed_and_reordered_rows(
    actual_rows: tuple[tuple[str, ...], ...], message: str
) -> None:
    expected = _accounts_projection(
        (
            ("alpha", "provider", "yes", "5", "$1.00"),
            ("beta", "provider", "no", "0", "$0.00"),
        )
    )
    actual = _accounts_projection(actual_rows)
    with pytest.raises(AssertionError, match=message):
        q.compare_dom_projection(expected, actual, "/accounts")


def test_q012_rejects_status_control_escaping_and_active_navigation_drift() -> None:
    expected_status = q.project_html(
        _shell(
            active="/models",
            title="Model: example-model",
            heading="Model: example-model",
            body='<p class="empty">Model info not available.</p>',
        )
    )
    changed_status = q.project_html(
        _shell(
            active="/models",
            title="Model: example-model",
            heading="Model: example-model",
            body='<p class="empty">Model info available.</p>',
        )
    )
    with pytest.raises(AssertionError, match="status messages"):
        q.compare_dom_projection(
            expected_status, changed_status, "/models/example-model"
        )

    expected_form = _accounts_projection((("alpha", "provider", "yes", "5", "$1.00"),))
    missing_control = _accounts_projection(
        (("alpha", "provider", "yes", "5", "$1.00"),), form=False
    )
    with pytest.raises(AssertionError, match="period"):
        q.compare_dom_projection(expected_form, missing_control, "/accounts")

    escaped = _accounts_projection(
        (("alpha &lt;safe&gt;", "provider", "yes", "5", "$1.00"),)
    )
    unescaped = _accounts_projection(
        (("alpha <safe>", "provider", "yes", "5", "$1.00"),)
    )
    with pytest.raises(AssertionError, match="table projection"):
        q.compare_dom_projection(escaped, unescaped, "/accounts")

    active = _accounts_projection((("alpha", "provider", "yes", "5", "$1.00"),))
    wrong_active = q.project_html(
        _shell(
            active="/models",
            body=_account_table((("alpha", "provider", "yes", "5", "$1.00"),)),
        )
    )
    with pytest.raises(AssertionError, match="active navigation"):
        q.compare_dom_projection(active, wrong_active, "/accounts")


def test_q012_live_matrix_covers_both_implementations_and_all_states() -> None:
    report = q.run_qualification(skip_build=True, include_screenshots=False)
    assert report["page_routes"] == [route for route, _label in q.PAGE_ROUTES]
    observations = report["dom_comparisons"]
    page_observations = [item for item in observations if "state" in item]
    assert {(item["state"], item["route"]) for item in page_observations} == {
        (state, route)
        for state in ("empty", "populated")
        for route, _label in q.PAGE_ROUTES
    }
    assert report["fixture"]["secrets"] == "none"
    assert report["fixture_matrix"][-1]["classes"] == [
        "unauthorized",
        "authorized",
        "all dashboard routes",
    ]
    assert len(json.dumps(report).encode()) < q.MAX_RESULT_BYTES
