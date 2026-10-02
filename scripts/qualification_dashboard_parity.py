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

from __future__ import annotations

if __package__:
    from .dashboard_parity._shared import *  # noqa: F403
    from .dashboard_parity.browser import *  # noqa: F403
    from .dashboard_parity.cli import main
    from .dashboard_parity.fixtures import *  # noqa: F403
    from .dashboard_parity.oracle import *  # noqa: F403
    from .dashboard_parity.process import *  # noqa: F403
    from .dashboard_parity.projection import *  # noqa: F403
    from .dashboard_parity.report import *  # noqa: F403
    from .dashboard_parity.runner import *  # noqa: F403
else:
    from dashboard_parity._shared import *  # noqa: F403
    from dashboard_parity.browser import *  # noqa: F403
    from dashboard_parity.cli import main
    from dashboard_parity.fixtures import *  # noqa: F403
    from dashboard_parity.oracle import *  # noqa: F403
    from dashboard_parity.process import *  # noqa: F403
    from dashboard_parity.projection import *  # noqa: F403
    from dashboard_parity.report import *  # noqa: F403
    from dashboard_parity.runner import *  # noqa: F403
if __name__ == "__main__":
    raise SystemExit(main())
