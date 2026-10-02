from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from scripts.qualification_dashboard_parity import (
    API_ROUTES,
    CAPTURES_DIR,
    ORACLE_COMMIT,
    ORACLE_DIR,
    PAGE_ROUTES,
    HttpResult,
    _route_filename,
    _wait_for_operational_event,
    build_oracle_manifest,
    compare_api_response,
    compare_dom_projection,
    compare_json_projection,
    compare_shared_shell_projection,
    project_html,
)


def test_oracle_operational_cache_barrier_requires_persisted_startup_event(tmp_path):
    database = tmp_path / "dashboard.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE operational_events "
            "(event_type TEXT NOT NULL, occurred_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO operational_events "
            "VALUES ('crash_recovery', '2000-01-01 00:00:00')"
        )

    class RunningProcess:
        stderr = None

        @staticmethod
        def poll():
            return None

    _wait_for_operational_event(
        database, RunningProcess(), "crash_recovery", "oracle fixture"
    )


@pytest.mark.parametrize(
    ("source", "mutation"),
    [
        (
            "<main><canvas id='chart'></canvas></main>",
            "<main><section id='chart'></section></main>",
        ),
        (
            "<main class='panel' data-kind='chart'></main>",
            "<main class='panel'></main>",
        ),
        ("<main class='panel'></main>", "<main class='pane'></main>"),
        ("<main id='content'></main>", "<main id='container'></main>"),
        ("<form><input name='period'></form>", "<form></form>"),
        (
            "<form><button type='submit'>Apply</button></form>",
            "<form><button>Apply</button></form>",
        ),
        (
            "<ul><li>first</li><li>second</li></ul>",
            "<ul><li>second</li><li>first</li></ul>",
        ),
        (
            "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>",
            "<p><script>alert(1)</script></p>",
        ),
        (
            "<section id='cards'><article>one</article></section>",
            "<section id='cards'></section>",
        ),
        (
            "<table><thead><tr><th>Name</th><th>Status</th></tr></thead><tbody><tr><td>A</td><td>Ready</td></tr></tbody></table>",
            "<table><thead><tr><th>Status</th><th>Name</th></tr></thead><tbody><tr><td>Ready</td><td>A</td></tr></tbody></table>",
        ),
        ("<a href='/models/a'>model</a>", "<a href='/models/b'>model</a>"),
        (
            "<script type='application/json'>{\"count\":1}</script>",
            "<script type='application/json'>{\"count\":2}</script>",
        ),
    ],
)
def test_complete_dom_projection_rejects_contract_drift(
    source: str, mutation: str
) -> None:
    expected = project_html(source)
    changed = project_html(mutation)

    with pytest.raises(AssertionError, match="complete DOM tree differs"):
        compare_dom_projection(expected, changed, "/fixture")


def test_complete_dom_projection_ignores_attribute_order() -> None:
    expected = project_html("<main id='content' class='page panel'></main>")
    actual = project_html("<main class='panel page' id='content'></main>")

    compare_dom_projection(expected, actual, "/fixture")


def test_complete_dom_projection_normalizes_documented_local_timestamps() -> None:
    expected = project_html("<main><time>2026-10-02 03:40:15</time></main>")
    actual = project_html("<main><time>2026-10-02 03:41:09</time></main>")
    compare_dom_projection(expected, actual, "/fixture")

    with pytest.raises(AssertionError, match="complete DOM tree differs"):
        compare_dom_projection(
            project_html("<main><time>2026-10-02 03:40</time></main>"),
            actual,
            "/fixture",
        )


def test_runtime_comparison_keeps_capture_metrics_normalized_and_labels_strict() -> (
    None
):
    expected = project_html(
        "<main><div class='card'><h3>Server PID</h3>"
        "<p class='metric'>12345</p></div>"
        "<div class='card'><h3>Requests</h3>"
        "<p class='metric'>12</p></div></main>"
    )
    different_host = project_html(
        "<main><div class='card'><h3>Server PID</h3>"
        "<p class='metric'>67890</p></div>"
        "<div class='card'><h3>Requests</h3>"
        "<p class='metric'>12</p></div></main>"
    )
    compare_dom_projection(expected, different_host, "/runtime")

    changed_label = project_html(
        "<main><div class='card'><h3>Server PID</h3>"
        "<p class='metric'>67890</p></div>"
        "<div class='card'><h3>Request count</h3>"
        "<p class='metric'>13</p></div></main>"
    )
    with pytest.raises(AssertionError, match="complete DOM tree differs"):
        compare_dom_projection(expected, changed_label, "/runtime")


def test_shared_shell_projection_ignores_page_body_and_rejects_shell_drift() -> None:
    expected = project_html(
        "<html><body><header class='topbar'><a href='/'>EggPool</a></header>"
        "<main id='dashboard-content'><h2>Overview</h2></main>"
        "<footer>ready</footer></body></html>"
    )
    page_change = project_html(
        "<html><body><header class='topbar'><a href='/'>EggPool</a></header>"
        "<main id='dashboard-content'><table><tr><td>Accounts</td></tr></table></main>"
        "<footer>ready</footer></body></html>"
    )
    compare_shared_shell_projection(expected, page_change, "/fixture")

    changed_shell = project_html(
        "<html><body><header class='topbar'><a href='/home'>EggPool</a></header>"
        "<main id='dashboard-content'><h2>Overview</h2></main>"
        "<footer>ready</footer></body></html>"
    )
    with pytest.raises(AssertionError, match="shared shell differs"):
        compare_shared_shell_projection(expected, changed_shell, "/fixture")


def test_complete_dom_projection_rejects_duplicate_ids_and_unsafe_links() -> None:
    duplicate = project_html("<main><span id='same'></span><b id='same'></b></main>")
    with pytest.raises(AssertionError, match="duplicate IDs"):
        compare_dom_projection(duplicate, duplicate, "/fixture")

    external = project_html("<main><a href='https://example.invalid/'>x</a></main>")
    with pytest.raises(AssertionError, match="unsafe links"):
        compare_dom_projection(external, external, "/fixture")


def test_frozen_manifest_identity_inventory_and_asset_blobs() -> None:
    checked_in = json.loads((ORACLE_DIR / "manifest.json").read_text())
    regenerated = build_oracle_manifest()

    assert checked_in == regenerated
    assert checked_in["oracle_commit"] == ORACLE_COMMIT
    assert len(checked_in["page_routes"]) == 14
    assert len(checked_in["dashboard_json_routes"]) == 8
    assert len(checked_in["themes"]) == 50
    hooks = checked_in["javascript_hooks"]
    assert set(hooks["api_endpoints"]) <= {
        route["path"] for route in checked_in["dashboard_json_routes"]
    }
    assert set(hooks["selectors"]) == set(hooks["selector_producers"]) | set(
        hooks["conditional_selector_producers"]
    )
    assert all(
        routes or selector in hooks["conditional_selector_producers"]
        for selector, routes in hooks["selector_producers"].items()
    )
    assert {asset["git_blob"] for asset in checked_in["assets"]} == {
        "5242e07e36da614698991ce884277416b9aedbab",
        "1a7ff048b435043a127414092ec92fdf7893f3c5",
        "0bae5b84bcf2dbd6d81aed3bc652f97eb9b00b1a",
        "a075c45b9d3a9938ade60eb399f18c0d9cd7383a",
    }
    assert len(json.dumps(checked_in)) < 32_768


def test_current_gap_report_is_bounded_and_diagnostic_only() -> None:
    report_path = ORACLE_DIR / "current-gap-report.json"
    report = json.loads(report_path.read_text())
    assert report["schema_version"] == "dashboard-parity-current-gaps.v1"
    assert report["candidate_sha"] == "3cf7671"
    assert report["parity_status"] == "gaps"
    assert report["mismatch_count"] > 0
    assert sum(report["mismatch_groups"].values()) == report["mismatch_count"]
    assert report_path.stat().st_size < 256 * 1024


def test_checked_in_oracle_captures_are_complete_bounded_and_sanitized() -> None:
    capture_manifest = json.loads((CAPTURES_DIR / "capture-manifest.json").read_text())
    assert capture_manifest["oracle_commit"] == ORACLE_COMMIT
    for relative, expected_hash in capture_manifest["files"].items():
        path = CAPTURES_DIR / relative
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash
        assert path.stat().st_size <= 256 * 1024

    for state in ("empty", "populated"):
        page_files = list((CAPTURES_DIR / state / "pages").glob("*.json"))
        api_files = list((CAPTURES_DIR / state / "api").glob("*.json"))
        assert {path.stem for path in page_files} == {
            _route_filename(route) for route, _label in PAGE_ROUTES
        }
        assert {path.stem for path in api_files} == {
            _route_filename(route) for route in API_ROUTES
        }
    private_auth = json.loads((CAPTURES_DIR / "private-auth.json").read_text())
    assert set(private_auth) == {route for route, _label in PAGE_ROUTES} | set(
        API_ROUTES
    )
    assert set(private_auth.values()) == {401}

    for path in CAPTURES_DIR.rglob("*.json"):
        assert "q012-server-key" not in path.read_text()
        assert "sk-proj-" not in path.read_text()
    assert capture_manifest["total_bytes"] < 2 * 1024 * 1024


def test_api_json_comparison_detects_missing_fields_and_order() -> None:
    expected = '{"bucket":"hour","rows":[{"name":"a"},{"name":"b"}]}'
    missing_field = '{"rows":[{"name":"a"},{"name":"b"}]}'
    reordered = '{"bucket":"hour","rows":[{"name":"b"},{"name":"a"}]}'

    with pytest.raises(AssertionError, match="JSON projection"):
        compare_json_projection(expected, missing_field, "/api/timeseries")
    with pytest.raises(AssertionError, match="JSON projection"):
        compare_json_projection(expected, reordered, "/api/timeseries")


def test_api_comparison_detects_missing_route_and_auth_status_drift() -> None:
    expected = HttpResult(200, {"content-type": "application/json"}, '{"ok":true}')
    missing_route = HttpResult(404, {"content-type": "text/plain"}, "not found")
    private_route = HttpResult(401, {"content-type": "application/json"}, "{}")

    with pytest.raises(AssertionError, match="status differs"):
        compare_api_response(expected, missing_route, "/api/timeseries")
    with pytest.raises(AssertionError, match="status differs"):
        compare_api_response(expected, private_route, "/api/timeseries")
