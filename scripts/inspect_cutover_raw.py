#!/usr/bin/env python3
"""Inspect one raw Rust release executable for the K003 artifact contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

MAX_RAW_BYTES: Final = 100_000_000
VERSION_RE: Final = re.compile(r"\d+\.\d+\.\d+(?:(?:a|b|rc|dev|post)\d*)?\Z")
RAW_NAME_RE: Final = re.compile(
    r"^eggpool-(?P<version>[^-]+)-(?P<os>linux|macos)-"
    r"(?P<arch>x86_64|aarch64)\Z"
)
TARGETS: Final = {
    "linux-x86_64": {
        "rust_target": "x86_64-unknown-linux-gnu",
        "os": "linux",
        "arch": "x86_64",
        "kind": "elf",
        "machine": 62,
    },
    "linux-aarch64": {
        "rust_target": "aarch64-unknown-linux-gnu",
        "os": "linux",
        "arch": "aarch64",
        "kind": "elf",
        "machine": 183,
    },
    "macos-arm64": {
        "rust_target": "aarch64-apple-darwin",
        "os": "macos",
        "arch": "aarch64",
        "kind": "macho",
        "machine": 0x0100000C,
    },
}


class RawInspectionError(ValueError):
    """A deterministic, secret-free raw artifact contract failure."""


@dataclass(frozen=True)
class RawInspection:
    """Bounded facts retained from a validated raw executable."""

    filename: str
    version: str
    target_class: str
    rust_target: str
    sha256: str
    size: int
    executable: bool


def _native_kind_and_arch(payload: bytes) -> tuple[str, int]:
    if payload.startswith(b"\x7fELF") and len(payload) >= 20:
        return "elf", struct.unpack_from("<H", payload, 18)[0]
    if payload[:4] in {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"} and len(payload) >= 8:
        endian = "<" if payload[:4] == b"\xcf\xfa\xed\xfe" else ">"
        return "macho", struct.unpack_from(f"{endian}I", payload, 4)[0]
    raise RawInspectionError("raw executable is not an ELF or 64-bit Mach-O binary")


def raw_filename(version: str, target_class: str) -> str:
    """Return the stable O008-compatible raw asset filename."""
    if not VERSION_RE.fullmatch(version):
        raise RawInspectionError("raw artifact version is invalid")
    target = TARGETS.get(target_class)
    if target is None:
        raise RawInspectionError(f"unsupported target class: {target_class}")
    return f"eggpool-{version}-{target['os']}-{target['arch']}"


def inspect_raw(
    path: Path,
    *,
    expected_version: str,
    target_class: str,
    max_size: int = MAX_RAW_BYTES,
) -> RawInspection:
    """Validate one raw executable against the K003 contract."""
    if not VERSION_RE.fullmatch(expected_version):
        raise RawInspectionError("expected version is invalid")
    target = TARGETS.get(target_class)
    if target is None:
        raise RawInspectionError(f"unsupported target class: {target_class}")
    if not path.is_file() or path.name != raw_filename(expected_version, target_class):
        raise RawInspectionError("raw filename does not match the target contract")
    size = path.stat().st_size
    if size <= 0 or size >= max_size:
        raise RawInspectionError("raw executable exceeds the configured size bound")
    if path.stat().st_mode & 0o111 == 0:
        raise RawInspectionError("raw executable does not retain executable mode")
    payload = path.read_bytes()
    kind, machine = _native_kind_and_arch(payload)
    if (kind, machine) != (target["kind"], target["machine"]):
        raise RawInspectionError("raw executable format disagrees with target class")
    return RawInspection(
        filename=path.name,
        version=expected_version,
        target_class=target_class,
        rust_target=str(target["rust_target"]),
        sha256=hashlib.sha256(payload).hexdigest(),
        size=size,
        executable=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--target-class", required=True, choices=sorted(TARGETS))
    args = parser.parse_args(argv)
    try:
        result = inspect_raw(
            args.raw,
            expected_version=args.version,
            target_class=args.target_class,
        )
    except RawInspectionError as error:
        print(f"K003 raw artifact invalid: {error}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
