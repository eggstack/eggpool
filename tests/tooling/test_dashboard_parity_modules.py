from __future__ import annotations

from scripts.dashboard_parity.browser import screenshot_plan
from scripts.dashboard_parity.fixtures import qualify_dashboard_shutdown_restart
from scripts.dashboard_parity.oracle import build_oracle_manifest
from scripts.dashboard_parity.process import _wait_for_operational_event
from scripts.dashboard_parity.projection import project_html
from scripts.dashboard_parity.report import write_report
from scripts.dashboard_parity.runner import run_qualification


def test_dashboard_parity_responsibilities_have_stable_internal_owners() -> None:
    assert project_html.__module__ == "scripts.dashboard_parity.projection"
    assert build_oracle_manifest.__module__ == "scripts.dashboard_parity.oracle"
    assert (
        qualify_dashboard_shutdown_restart.__module__
        == "scripts.dashboard_parity.fixtures"
    )
    assert _wait_for_operational_event.__module__ == "scripts.dashboard_parity.process"
    assert screenshot_plan.__module__ == "scripts.dashboard_parity.browser"
    assert write_report.__module__ == "scripts.dashboard_parity.report"
    assert run_qualification.__module__ == "scripts.dashboard_parity.runner"
