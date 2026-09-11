#!/usr/bin/env python3
"""Inspect an EggPool Rust release wheel using only the standard library."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import re
import struct
import sys
import zipfile
from dataclasses import asdict, dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from email.message import Message

MAX_WHEEL_BYTES: Final = 100_000_000
VERSION_RE: Final = re.compile(r"\d+\.\d+\.\d+(?:(?:a|b|rc|dev|post)\d*)?\Z")
WHEEL_NAME_RE: Final = re.compile(
    r"^eggpool-(?P<version>[^-]+)-(?P<python>[^-]+)-"
    r"(?P<abi>[^-]+)-(?P<platform>[^.]+(?:\.[^.]+)*)\.whl\Z"
)
TARGET_PLATFORMS: Final = {
    "linux-x86_64": frozenset({"manylinux2014_x86_64", "manylinux_2_17_x86_64"}),
    "linux-aarch64": frozenset({"manylinux2014_aarch64", "manylinux_2_17_aarch64"}),
    "macos-arm64": frozenset({"macosx_11_0_arm64"}),
}
TARGET_ARCHES: Final = {
    "linux-x86_64": ("elf", 62),
    "linux-aarch64": ("elf", 183),
    "macos-arm64": ("macho", 0x0100000C),
}


class WheelInspectionError(ValueError):
    """A deterministic, secret-free wheel contract failure."""


@dataclass(frozen=True)
class WheelInspection:
    """The bounded facts retained from a validated wheel."""

    filename: str
    version: str
    tags: tuple[str, ...]
    target_class: str
    executable_member: str
    wheel_size: int
    executable_size: int
    members: tuple[str, ...]
    dependencies: tuple[str, ...]
    requires_python: str


def _metadata_value(message: Message, name: str) -> str:
    value = message.get(name)
    if not isinstance(value, str) or not value.strip():
        raise WheelInspectionError(f"wheel metadata is missing {name}")
    return value.strip()


def _native_kind_and_arch(payload: bytes) -> tuple[str, int]:
    if payload.startswith(b"\x7fELF") and len(payload) >= 20:
        return "elf", struct.unpack_from("<H", payload, 18)[0]
    if payload[:4] in {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"} and len(payload) >= 8:
        endian = "<" if payload[:4] == b"\xcf\xfa\xed\xfe" else ">"
        return "macho", struct.unpack_from(f"{endian}I", payload, 4)[0]
    raise WheelInspectionError("wheel executable is not an ELF or 64-bit Mach-O binary")


def _validate_record(
    record: bytes, members: dict[str, bytes], executable_member: str
) -> None:
    rows = list(csv.reader(io.StringIO(record.decode("utf-8"))))
    record_names = {row[0] for row in rows if row}
    if record_names != set(members):
        raise WheelInspectionError("RECORD inventory does not match wheel members")
    for row in rows:
        if len(row) != 3:
            raise WheelInspectionError("wheel RECORD contains a malformed row")
        name, digest, size = row
        if not name:
            raise WheelInspectionError("wheel RECORD contains an invalid member")
        if name.endswith(".dist-info/RECORD"):
            if digest or size:
                raise WheelInspectionError("RECORD must not hash itself")
            continue
        expected_size = str(len(members[name]))
        if size != expected_size:
            raise WheelInspectionError(f"RECORD size mismatch for {name}")
        if not digest.startswith("sha256="):
            raise WheelInspectionError(f"RECORD hash is not SHA-256 for {name}")
        encoded = base64.urlsafe_b64encode(hashlib.sha256(members[name]).digest())
        if digest.removeprefix("sha256=") != encoded.rstrip(b"=").decode("ascii"):
            raise WheelInspectionError(f"RECORD hash mismatch for {name}")
    if executable_member not in record_names:
        raise WheelInspectionError("wheel executable is absent from RECORD")


def inspect_wheel(
    path: Path,
    *,
    expected_version: str,
    target_class: str,
    max_size: int = MAX_WHEEL_BYTES,
) -> WheelInspection:
    """Validate one wheel against the release packaging binary-wheel contract."""

    if not VERSION_RE.fullmatch(expected_version):
        raise WheelInspectionError(
            "expected version is not a supported release version"
        )
    if target_class not in TARGET_PLATFORMS:
        raise WheelInspectionError(f"unsupported target class: {target_class}")
    if not path.is_file() or path.suffix != ".whl":
        raise WheelInspectionError("wheel path does not name a wheel file")
    wheel_size = path.stat().st_size
    if wheel_size <= 0 or wheel_size >= max_size:
        raise WheelInspectionError("wheel exceeds the configured PyPI file-size bound")

    match = WHEEL_NAME_RE.fullmatch(path.name)
    if match is None:
        raise WheelInspectionError("wheel filename is not a normalized eggpool wheel")
    if match.group("version") != expected_version:
        raise WheelInspectionError("wheel filename version disagrees with candidate")
    platform_tags = tuple(match.group("platform").split("."))
    if not set(platform_tags) <= TARGET_PLATFORMS[target_class]:
        raise WheelInspectionError("wheel platform tag disagrees with target class")
    if "any" in platform_tags:
        raise WheelInspectionError("Rust release wheel must not be universal")

    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise WheelInspectionError("wheel contains duplicate member names")
            if any(
                name.startswith(("/", "\\")) or ".." in Path(name).parts
                for name in names
            ):
                raise WheelInspectionError("wheel contains an unsafe member path")
            members = {name: archive.read(name) for name in names}
            dist_infos = [
                name for name in names if name.endswith(".dist-info/METADATA")
            ]
            wheel_infos = [name for name in names if name.endswith(".dist-info/WHEEL")]
            records = [name for name in names if name.endswith(".dist-info/RECORD")]
            if not (len(dist_infos) == len(wheel_infos) == len(records) == 1):
                raise WheelInspectionError(
                    "wheel must contain one complete dist-info set"
                )
            metadata_name = dist_infos[0]
            dist_info_prefix = metadata_name.removesuffix("METADATA")
            if not all(
                name.startswith(dist_info_prefix)
                for name in (wheel_infos[0], records[0])
            ):
                raise WheelInspectionError(
                    "wheel dist-info members do not share a prefix"
                )
            metadata = BytesParser(policy=policy.compat32).parsebytes(
                members[metadata_name]
            )
            if _metadata_value(metadata, "Name").lower() != "eggpool":
                raise WheelInspectionError("wheel project name is not eggpool")
            if _metadata_value(metadata, "Version") != expected_version:
                raise WheelInspectionError(
                    "wheel metadata version disagrees with candidate"
                )
            if _metadata_value(metadata, "Requires-Python") != ">=3.11":
                raise WheelInspectionError("wheel Requires-Python is not >=3.11")
            if not _metadata_value(metadata, "Summary"):
                raise WheelInspectionError("wheel metadata has no project summary")
            license_files = metadata.get_all("License-File", [])
            if not any(value.lower().endswith("license") for value in license_files):
                raise WheelInspectionError(
                    "wheel metadata does not identify the MIT license"
                )
            if not any(
                name.endswith(value.lstrip("/"))
                for value in license_files
                for name in names
            ):
                raise WheelInspectionError("wheel license metadata points to no file")
            dependencies = tuple(metadata.get_all("Requires-Dist", []))
            if dependencies:
                raise WheelInspectionError(
                    "Rust release wheel declares Python dependencies"
                )
            wheel_metadata = BytesParser(policy=policy.compat32).parsebytes(
                members[wheel_infos[0]]
            )
            if _metadata_value(wheel_metadata, "Root-Is-Purelib").lower() != "false":
                raise WheelInspectionError("wheel is marked as pure Python")
            tags = tuple(wheel_metadata.get_all("Tag", []))
            if not tags or not set(platform_tags) <= {
                tag.rsplit("-", 1)[-1] for tag in tags
            }:
                raise WheelInspectionError(
                    "WHEEL metadata has no matching platform tag"
                )
            payload_names = [
                name for name in names if not name.startswith(dist_info_prefix)
            ]
            executable_names = [
                name for name in payload_names if Path(name).name == "eggpool"
            ]
            if executable_names != [f"eggpool-{expected_version}.data/scripts/eggpool"]:
                raise WheelInspectionError(
                    "wheel must contain exactly one scripts/eggpool payload"
                )
            executable_member = executable_names[0]
            executable_info = archive.getinfo(executable_member)
            if executable_info.external_attr >> 16 & 0o111 == 0:
                raise WheelInspectionError(
                    "wheel executable does not retain executable mode"
                )
            kind, arch = _native_kind_and_arch(members[executable_member])
            expected_kind, expected_arch = TARGET_ARCHES[target_class]
            if (kind, arch) != (expected_kind, expected_arch):
                raise WheelInspectionError(
                    "wheel executable format disagrees with target class"
                )
            _validate_record(members[records[0]], members, executable_member)
            return WheelInspection(
                filename=path.name,
                version=expected_version,
                tags=tags,
                target_class=target_class,
                executable_member=executable_member,
                wheel_size=wheel_size,
                executable_size=len(members[executable_member]),
                members=tuple(sorted(names)),
                dependencies=dependencies,
                requires_python=">=3.11",
            )
    except zipfile.BadZipFile as error:
        raise WheelInspectionError("wheel is not a valid ZIP archive") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--target-class", required=True, choices=sorted(TARGET_PLATFORMS)
    )
    args = parser.parse_args(argv)
    try:
        result = inspect_wheel(
            args.wheel,
            expected_version=args.version,
            target_class=args.target_class,
        )
    except WheelInspectionError as error:
        print(f"release packaging wheel invalid: {error}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
