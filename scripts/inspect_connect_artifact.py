#!/usr/bin/env python3
"""Inspect one `eggpool-connect` desktop helper artifact.

The helper is a small native desktop configurator, not a second EggPool
proxy. Its release identity is intentionally distinct from the proxy
wheel/raw matrix (`inspect_release_raw.TARGETS`): helper filenames carry the
`eggpool-connect-` product prefix, helper target classes carry the `connect-`
prefix, and the proxy "exactly three raw artifacts" invariant is unaffected.
"""

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

MAX_CONNECT_BYTES: Final = 50_000_000
MAX_BOOTSTRAP_BYTES: Final = 65_536
VERSION_RE: Final = re.compile(r"\d+\.\d+\.\d+(?:(?:a|b|rc|dev|post)\d*)?\Z")
BOOTSTRAP_NAME_RE: Final = re.compile(r"^eggpool-connect\.(sh|ps1)\Z")

CONNECT_TARGETS: Final = {
    "connect-linux-x86_64": {
        "rust_target": "x86_64-unknown-linux-gnu",
        "os": "linux",
        "arch": "x86_64",
        "kind": "elf",
        "machine": 62,
        "exe_suffix": "",
    },
    "connect-linux-aarch64": {
        "rust_target": "aarch64-unknown-linux-gnu",
        "os": "linux",
        "arch": "aarch64",
        "kind": "elf",
        "machine": 183,
        "exe_suffix": "",
    },
    "connect-macos-arm64": {
        "rust_target": "aarch64-apple-darwin",
        "os": "macos",
        "arch": "aarch64",
        "kind": "macho",
        "machine": 0x0100000C,
        "exe_suffix": "",
    },
    "connect-windows-x86_64": {
        "rust_target": "x86_64-pc-windows-msvc",
        "os": "windows",
        "arch": "x86_64",
        "kind": "pe",
        "machine": 0,
        "exe_suffix": ".exe",
    },
}

CONNECT_BOOTSTRAPS: Final = ("eggpool-connect.sh", "eggpool-connect.ps1")


class ConnectInspectionError(ValueError):
    """A deterministic, secret-free helper artifact contract failure."""


@dataclass(frozen=True)
class ConnectInspection:
    """Bounded facts retained from a validated helper executable."""

    filename: str
    version: str
    target_class: str
    rust_target: str
    kind: str
    sha256: str
    size: int
    executable: bool


def _native_kind_and_arch(payload: bytes) -> tuple[str, int]:
    if payload.startswith(b"\x7fELF") and len(payload) >= 20:
        return "elf", struct.unpack_from("<H", payload, 18)[0]
    if payload[:4] in {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"} and len(payload) >= 8:
        endian = "<" if payload[:4] == b"\xcf\xfa\xed\xfe" else ">"
        return "macho", struct.unpack_from(f"{endian}I", payload, 4)[0]
    if payload.startswith(b"MZ") and len(payload) >= 64:
        return "pe", 0
    raise ConnectInspectionError("helper executable is not ELF, Mach-O, or PE")


def connect_filename(version: str, target_class: str) -> str:
    """Return the stable helper asset filename for one helper target class."""
    if not VERSION_RE.fullmatch(version):
        raise ConnectInspectionError("helper artifact version is invalid")
    target = CONNECT_TARGETS.get(target_class)
    if target is None:
        raise ConnectInspectionError(f"unsupported helper target class: {target_class}")
    stem = f"eggpool-connect-{version}-{target['os']}-{target['arch']}"
    return f"{stem}{target['exe_suffix']}"


def inspect_connect_artifact(
    path: Path,
    *,
    expected_version: str,
    target_class: str,
    max_size: int = MAX_CONNECT_BYTES,
) -> ConnectInspection:
    """Validate one helper executable against the helper artifact contract."""
    if not VERSION_RE.fullmatch(expected_version):
        raise ConnectInspectionError("expected version is invalid")
    target = CONNECT_TARGETS.get(target_class)
    if target is None:
        raise ConnectInspectionError(f"unsupported helper target class: {target_class}")
    if not path.is_file() or path.name != connect_filename(
        expected_version, target_class
    ):
        raise ConnectInspectionError(
            "helper filename does not match the target contract"
        )
    size = path.stat().st_size
    if size <= 0 or size >= max_size:
        raise ConnectInspectionError(
            "helper executable exceeds the configured size bound"
        )
    if target["os"] != "windows" and path.stat().st_mode & 0o111 == 0:
        raise ConnectInspectionError(
            "helper executable does not retain executable mode"
        )
    payload = path.read_bytes()
    kind, machine = _native_kind_and_arch(payload)
    if kind != target["kind"]:
        raise ConnectInspectionError(
            "helper executable format disagrees with target class"
        )
    if kind != "pe" and machine != target["machine"]:
        raise ConnectInspectionError(
            "helper executable arch disagrees with target class"
        )
    return ConnectInspection(
        filename=path.name,
        version=expected_version,
        target_class=target_class,
        rust_target=str(target["rust_target"]),
        kind="eggpool-connect",
        sha256=hashlib.sha256(payload).hexdigest(),
        size=size,
        executable=True,
    )


def inspect_connect_bootstrap(path: Path) -> dict[str, object]:
    """Validate one bootstrap script (reviewed static file, no build output)."""
    if not BOOTSTRAP_NAME_RE.fullmatch(path.name):
        raise ConnectInspectionError(f"unsupported bootstrap filename: {path.name}")
    if not path.is_file():
        raise ConnectInspectionError(f"bootstrap script is missing: {path.name}")
    payload = path.read_bytes()
    if not payload or len(payload) > MAX_BOOTSTRAP_BYTES:
        raise ConnectInspectionError(
            "bootstrap script exceeds the configured size bound"
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ConnectInspectionError("bootstrap script is not valid UTF-8") from error
    if "\0" in text:
        raise ConnectInspectionError("bootstrap script contains NUL bytes")
    return {
        "kind": "connect-bootstrap",
        "filename": path.name,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "executable": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--target-class", required=True, choices=sorted(CONNECT_TARGETS)
    )
    args = parser.parse_args(argv)
    try:
        result = inspect_connect_artifact(
            args.artifact,
            expected_version=args.version,
            target_class=args.target_class,
        )
    except ConnectInspectionError as error:
        print(f"helper artifact invalid: {error}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
