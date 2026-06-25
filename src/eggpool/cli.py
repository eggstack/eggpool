"""CLI bootstrap.

This module is intentionally tiny. ``main()`` first tries the
stdlib-only fast-path dispatcher (:mod:`eggpool.fastcli`) for
``croncheck`` and ``ensure-running``, which avoids importing the full
application graph on Raspberry Pi-class hardware where every cron
invocation matters.

For everything else, the full Click CLI in :mod:`eggpool.cli_full` is
loaded lazily.

Public symbols (``cli`` and helpers used by tests) are forwarded lazily
from :mod:`eggpool.cli_full` via PEP 562 ``__getattr__``.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from collections.abc import Sequence


_LAZY_ATTRS: frozenset[str] = frozenset(
    {
        "cli",
        "generate_api_key",
        "write_server_api_key",
        "_is_process_running",
        "_read_pid",
        "_wait_for_exit",
        "_app_loader",
        "_read_server_api_key",
        "_update_server_config",
        "_detect_lan_ip",
        "_read_dashboard_public",
        "_write_dashboard_public",
        "_check_stale_contracts",
        "_detect_install_method",
    }
)


def __getattr__(name: str) -> Any:
    """Lazily forward attribute access to :mod:`eggpool.cli_full`.

    Keeps ``from eggpool.cli import cli`` working for tests without
    forcing the heavy CLI graph to load at :mod:`eggpool.cli` import
    time.
    """
    if name in _LAZY_ATTRS:
        from eggpool import cli_full

        value = getattr(cli_full, name)
        globals()[name] = value
        return value
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


def main(argv: Sequence[str] | None = None) -> NoReturn:
    """Entry point for the ``eggpool`` console script and ``python -m eggpool``."""
    from eggpool.fastcli import maybe_run_fast_command

    args = list(argv) if argv is not None else sys.argv[1:]
    code = maybe_run_fast_command(args)
    if code is not None:
        raise SystemExit(code)

    from eggpool.cli_full import cli as _cli

    _cli(obj={})
    raise SystemExit(0)
