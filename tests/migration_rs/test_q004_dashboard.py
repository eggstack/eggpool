"""Pure and inventory-level regression tests for the Q004 dashboard runner."""

from __future__ import annotations

import pytest

from scripts.qualification_dashboard import (
    MAX_RESULT_BYTES,
    NAV_PATHS,
    PAGE_ROUTES,
    asset_inventory,
    compare_dom_projection,
    project_html,
    screenshot_metadata,
    theme_inventory,
)


def _page(active: str = "/") -> str:
    links = "".join(
        f'<a class="{"active" if path == active else ""}" href="{path}">{path}</a>'
        for path in NAV_PATHS
    )
    return f"""<!doctype html><html><head><title>Overview</title></head><body>
    <nav><div id="topnav-menu">{links}</div></nav>
    <main id="dashboard-content"><h2>Overview</h2>
    <form method="get"><select id="period" name="period"></select></form>
    <link rel="stylesheet" href="/static/dashboard.css">
    <span id="dashboard-updated">ready</span></main></body></html>"""


def test_q004_dom_comparison_rejects_deliberate_semantic_mismatch() -> None:
    expected = project_html(_page())
    changed = project_html(_page("/accounts"))
    with pytest.raises(AssertionError, match="active navigation"):
        compare_dom_projection(expected, changed, "/")


def test_q004_page_and_theme_inventory_is_complete() -> None:
    assert len(PAGE_ROUTES) == 14
    inventory = asset_inventory()
    assert inventory["count"] == 54
    themes = theme_inventory()
    assert themes["count"] == 51
    assert "Cyberpunk" in themes["review_set"]


def test_q004_screenshot_metadata_is_deterministic_and_bounded() -> None:
    first = screenshot_metadata()
    second = screenshot_metadata()
    assert first == second
    assert len(str(first).encode()) < MAX_RESULT_BYTES
    assert len(first["entries"]) == 14 * 2 * 4 * 2
    assert first["entries"][0]["artifact"].startswith("q004/")
