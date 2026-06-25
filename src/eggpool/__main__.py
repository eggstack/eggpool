"""Allow running as `python -m eggpool`."""

from __future__ import annotations

import sys

from eggpool.cli import main

main(sys.argv[1:])
