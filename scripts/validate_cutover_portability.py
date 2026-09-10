#!/usr/bin/env python3
"""Check one K003 raw executable's target-specific portability contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from inspect_cutover_raw import TARGETS, RawInspectionError, inspect_raw
from validate_cutover_artifacts import (
    ValidationError,
    linux_portability_evidence,
    macos_portability_evidence,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--target-class", required=True, choices=sorted(TARGETS))
    args = parser.parse_args(argv)
    try:
        inspect_raw(
            args.raw,
            expected_version=args.version,
            target_class=args.target_class,
        )
        evidence = (
            linux_portability_evidence(args.raw)
            if args.target_class.startswith("linux-")
            else macos_portability_evidence(args.raw)
        )
    except (RawInspectionError, ValidationError, OSError) as error:
        print(f"K003 portability check failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "pass", **evidence}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
