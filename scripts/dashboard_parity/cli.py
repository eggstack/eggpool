"""Run dashboard parity qualification against the frozen Python oracle.

The runner deliberately keeps browser work outside the Rust dependency graph.
Its normal run compares isolated Python and Rust HTTP servers, while
``--screenshots`` performs browser-backed captures and fails if an expected
artifact is not created. Browser work remains outside the Rust dependency
graph; the capture manifest is bounded and records hashes and dimensions.

Usage::

    uv run python scripts/qualification_dashboard_parity.py --skip-build
    uv run python scripts/qualification_dashboard_parity.py --skip-build --screenshots
"""

# ruff: noqa: F405
from __future__ import annotations

from ._shared import *  # noqa: F403
from .fixtures import *  # noqa: F403
from .oracle import *  # noqa: F403
from .process import *  # noqa: F403
from .report import *  # noqa: F403
from .runner import *  # noqa: F403


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-manifest",
        action="store_true",
        help=(
            "write the fixed oracle source and asset inventory without running servers"
        ),
    )
    parser.add_argument(
        "--capture-oracle",
        type=Path,
        help="capture synthetic responses from the pinned historical worktree",
    )
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--screenshots", action="store_true")
    parser.add_argument(
        "--shutdown-restart",
        action="store_true",
        help="qualify browser activity, bounded SIGTERM, and restart only",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--screenshot-dir", type=Path)
    options = parser.parse_args()
    try:
        if options.write_manifest:
            write_oracle_manifest()
            print(f"Dashboard oracle manifest written: {ORACLE_DIR / 'manifest.json'}")
            return 0
        if options.capture_oracle is not None:
            capture_oracle_snapshots(options.capture_oracle)
            print(f"Dashboard oracle captures written: {options.capture_oracle}")
            return 0
        if options.shutdown_restart:
            if not options.skip_build:
                result = subprocess.run(
                    ["cargo", "build", "--manifest-path", str(RUST_MANIFEST)],
                    cwd=ROOT,
                    check=False,
                )
                if result.returncode != 0:
                    raise QualificationError("Rust dashboard candidate failed to build")
            print(json.dumps(qualify_dashboard_shutdown_restart(), indent=2))
            return 0
        report = run_qualification(
            skip_build=options.skip_build,
            include_screenshots=options.screenshots,
            screenshot_dir=options.screenshot_dir,
        )
        write_report(report, options.output, options.markdown)
    except (AssertionError, QualificationError, OSError, ValueError) as error:
        print(f"Dashboard qualification failed: {error}", file=sys.stderr)
        return 1
    print(f"Dashboard qualification completed: {options.output}")
    return 0
