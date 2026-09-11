#!/usr/bin/env python3
"""Verify public PyPI/GitHub metadata against a release artifacts release manifest."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

JsonObject = dict[str, Any]


class PublicationVerificationError(ValueError):
    """Public release metadata does not match the immutable release manifest."""


def _load_json(path: Path) -> JsonObject:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PublicationVerificationError(f"could not read JSON: {path}") from error
    if not isinstance(value, dict):
        raise PublicationVerificationError(f"JSON root is not an object: {path}")
    return cast("JsonObject", value)


def _fetch_json(url: str) -> JsonObject:
    request = urllib.request.Request(
        url, headers={"User-Agent": "eggpool-release-verifier/1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read(4 * 1024 * 1024 + 1)
    except (OSError, urllib.error.URLError) as error:
        raise PublicationVerificationError(
            "public release metadata request failed"
        ) from error
    if len(body) > 4 * 1024 * 1024:
        raise PublicationVerificationError(
            "public release metadata exceeds the bounded response limit"
        )
    try:
        value = json.loads(body)
    except json.JSONDecodeError as error:
        raise PublicationVerificationError(
            "public release metadata is not JSON"
        ) from error
    if not isinstance(value, dict):
        raise PublicationVerificationError(
            "public release metadata root is not an object"
        )
    return cast("JsonObject", value)


def _manifest_records(manifest: JsonObject) -> tuple[str, list[JsonObject]]:
    version = manifest.get("release_version")
    records = manifest.get("artifacts")
    if not isinstance(version, str) or not isinstance(records, list) or not records:
        raise PublicationVerificationError("release manifest is incomplete")
    typed: list[JsonObject] = []
    for record in cast("list[Any]", records):
        if not isinstance(record, dict):
            raise PublicationVerificationError(
                "release manifest contains an invalid artifact record"
            )
        typed.append(cast("JsonObject", record))
    return version, typed


def verify_publication(
    manifest: JsonObject, pypi: JsonObject, github: JsonObject
) -> dict[str, object]:
    version, records = _manifest_records(manifest)
    info = pypi.get("info")
    if not isinstance(info, dict) or cast("JsonObject", info).get("version") != version:
        raise PublicationVerificationError(
            "PyPI version does not match the release manifest"
        )
    raw_urls = pypi.get("urls")
    if not isinstance(raw_urls, list):
        raise PublicationVerificationError("PyPI metadata has no file list")
    pypi_files: dict[str, JsonObject] = {}
    for value in cast("list[Any]", raw_urls):
        if isinstance(value, dict) and isinstance(
            cast("JsonObject", value).get("filename"), str
        ):
            item = cast("JsonObject", value)
            pypi_files[cast("str", item["filename"])] = item
    if any(str(name).endswith((".tar.gz", ".zip")) for name in pypi_files):
        raise PublicationVerificationError("PyPI publication contains a source archive")
    expected_wheels = {
        cast("JsonObject", record["wheel"])["filename"] for record in records
    }
    if set(pypi_files) != expected_wheels:
        raise PublicationVerificationError(
            "PyPI file set contains a missing or unsupported file"
        )
    for record in records:
        wheel = cast("JsonObject", record["wheel"])
        observed = pypi_files[wheel["filename"]]
        digest_value = observed.get("digests")
        if not isinstance(digest_value, dict):
            raise PublicationVerificationError(
                f"PyPI digest is missing: {wheel['filename']}"
            )
        digest = cast("JsonObject", digest_value)
        if digest.get("sha256") != wheel.get("sha256"):
            raise PublicationVerificationError(
                f"PyPI hash mismatch: {wheel['filename']}"
            )
    if (
        github.get("tag_name") != f"v{version}"
        or github.get("draft") is not False
        or github.get("prerelease") is not False
    ):
        raise PublicationVerificationError(
            "GitHub release is not the expected stable tag"
        )
    raw_assets = github.get("assets")
    if not isinstance(raw_assets, list):
        raise PublicationVerificationError("GitHub release has no asset list")
    assets: dict[str, JsonObject] = {}
    for value in cast("list[Any]", raw_assets):
        if isinstance(value, dict) and isinstance(
            cast("JsonObject", value).get("name"), str
        ):
            item = cast("JsonObject", value)
            assets[cast("str", item["name"])] = item
    expected_raw = {cast("JsonObject", record["raw"])["filename"] for record in records}
    expected_release_files = expected_raw | {
        "SHA256SUMS",
        f"eggpool-{version}-release-manifest.json",
    }
    if not expected_raw.issubset(assets):
        raise PublicationVerificationError(
            "GitHub release is missing a raw target asset"
        )
    for name in expected_raw:
        digest = assets[name].get("digest")
        expected = next(
            cast("dict[str, Any]", record["raw"])["sha256"]
            for record in records
            if cast("dict[str, Any]", record["raw"])["filename"] == name
        )
        if digest != f"sha256:{expected}":
            raise PublicationVerificationError(
                f"GitHub asset digest missing or mismatched: {name}"
            )
    unexpected = sorted(set(assets) - expected_release_files)
    if unexpected:
        raise PublicationVerificationError(
            f"GitHub release contains an unsupported asset: {unexpected[0]}"
        )
    return {
        "status": "pass",
        "version": version,
        "wheels": len(expected_wheels),
        "raw_assets": len(expected_raw),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--pypi-json")
    parser.add_argument("--github-json")
    args = parser.parse_args(argv)
    try:
        manifest = _load_json(args.manifest.resolve())
        pypi = (
            _load_json(Path(args.pypi_json))
            if args.pypi_json
            else _fetch_json("https://pypi.org/pypi/eggpool/json")
        )
        github = (
            _load_json(Path(args.github_json))
            if args.github_json
            else _fetch_json(
                "https://api.github.com/repos/eggstack/eggpool/releases/latest"
            )
        )
        print(json.dumps(verify_publication(manifest, pypi, github), sort_keys=True))
    except (OSError, PublicationVerificationError) as error:
        print(
            f"release workflow publication verification failed: {error}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
