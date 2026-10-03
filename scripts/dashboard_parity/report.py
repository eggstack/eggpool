# ruff: noqa: F405
from __future__ import annotations

from ._shared import *  # noqa: F403


def _mismatch_groups(observations: list[dict[str, Any]]) -> dict[str, int]:
    groups = {
        "shared-shell-and-api": 0,
        "overview-account-model": 0,
        "telemetry-routing-trace": 0,
        "runtime-cache": 0,
    }
    core_routes = {"/", "/accounts", "/models", "/models/example-model"}
    runtime_routes = {"/runtime", "/cache"}
    for row in observations:
        if row["status"] != "mismatch":
            continue
        route = str(row["route"])
        if row.get("kind") in {"api", "auth"}:
            group = "shared-shell-and-api"
        elif route in core_routes:
            group = "overview-account-model"
        elif route in runtime_routes:
            group = "runtime-cache"
        else:
            group = "telemetry-routing-trace"
        groups[group] += 1
    return groups


def _git_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or "unknown"


def write_report(report: dict[str, Any], json_path: Path, markdown_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    rows = [
        "# Dashboard M001 Current Gap Report",
        "",
        f"- Candidate: `{report['candidate_sha']}`",
        f"- Pages: `{len(report['page_routes'])}`",
        f"- Static/theme assets: `{report['static_assets']['count']}`",
        f"- Duration: `{report['duration_ms']} ms`",
        "",
        f"Disposition: `{report['parity_status']}`; "
        f"{report['mismatch_count']} matrix cells contain parity gaps.",
        "",
        "Mismatch groups: "
        + ", ".join(
            f"{group}={count}"
            for group, count in cast(
                "dict[str, int]", report["mismatch_groups"]
            ).items()
        ),
        "",
        "Each mismatch records a route/state and structural category or HTTP "
        "status only; response bodies and fixture values are excluded.",
    ]
    markdown_path.write_text("\n".join(rows) + "\n", encoding="utf-8")


__all__ = ["_git_sha", "_mismatch_groups", "write_report"]
