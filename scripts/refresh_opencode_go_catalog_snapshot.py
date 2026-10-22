"""Refresh the dated OpenCode Go catalog snapshot used by the drift guard.

The snapshot records the model ids the public OpenCode Go catalog served on a
given day. It is **review evidence**, not provider-profile metadata: the
canonical profile asset remains the single editable source of provider facts,
and this script never edits that asset or the reviewed endpoint-table record.

The refresh is a manual step. It is never invoked by CI, it never attaches a
credential, it prints the delta it introduces so a reviewer can see it before
committing, and it writes atomically so two concurrent refreshes cannot
interleave into one snapshot. Committing the rewritten snapshot is the human
decision.

Usage::

    uv run python scripts/refresh_opencode_go_catalog_snapshot.py
    uv run python scripts/refresh_opencode_go_catalog_snapshot.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
REVIEW_DIRECTORY: Final[Path] = (
    ROOT / "rust" / "crates" / "eggpool-provider-profile" / "review"
)
SNAPSHOT_PATH: Final[Path] = REVIEW_DIRECTORY / "opencode_go_catalog_snapshot.json"
CATALOG_URL: Final[str] = "https://opencode.ai/zen/go/v1/models"
SNAPSHOT_VERSION: Final[str] = "opencode-go-catalog.v1"
PROVIDER_ID: Final[str] = "opencode-go"
REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0
#: The upstream edge answers an ordinary request but rejects the default
#: `Python-urllib` agent with 403, so the refresh identifies itself.
USER_AGENT: Final[str] = "opencode-go-catalog-snapshot/1"
#: Platform trust stores to fall back to when the interpreter's OpenSSL
#: default verify paths resolve to an empty directory, which is the case for
#: the repository venv interpreter on macOS.
PLATFORM_CA_BUNDLES: Final[tuple[str, ...]] = (
    "/etc/ssl/cert.pem",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
)
CANONICAL_ASSET_NAMES: Final[frozenset[str]] = frozenset(
    {
        "_provider_profiles.toml",
        "provider_profiles.toml",
        "providers.toml",
        "_providers.toml",
        "_templates.toml",
    }
)


def build_ssl_context() -> ssl.SSLContext:
    """Return a TLS context that trusts the platform certificate authorities."""
    context = ssl.create_default_context()
    if context.get_ca_certs():
        return context
    for candidate in PLATFORM_CA_BUNDLES:
        bundle = Path(candidate)
        if not bundle.is_file():
            continue
        context.load_verify_locations(cafile=str(bundle))
        if context.get_ca_certs():
            return context
    raise RuntimeError("no platform certificate authority bundle is available")


def fetch_model_ids(url: str) -> list[str]:
    """Read the public catalog and return its sorted, de-duplicated model ids.

    No credential is attached: the endpoint answers `200` to an unauthenticated
    read, and a credentialed read would not prove anything extra.
    """
    request = Request(
        url,
        method="GET",
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urlopen(
            request, timeout=REQUEST_TIMEOUT_SECONDS, context=build_ssl_context()
        ) as response:
            status = int(response.status)
            payload = bytes(response.read())
    except HTTPError as error:
        raise RuntimeError(f"catalog answered HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError(f"catalog is unreachable: {error.reason}") from error
    if status != 200:
        raise RuntimeError(f"catalog answered HTTP {status}")
    document: object = json.loads(payload.decode("utf-8"))
    return extract_model_ids(document)


def extract_model_ids(document: object) -> list[str]:
    """Return the model ids carried by an OpenAI-shaped catalog document."""
    if not isinstance(document, dict):
        raise ValueError("catalog document is not an object")
    payload = cast("dict[str, object]", document)
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError("catalog document carries no data array")
    identifiers: set[str] = set()
    for entry in cast("list[object]", data):
        if not isinstance(entry, dict):
            raise ValueError("catalog entry is not an object")
        fields = cast("dict[str, object]", entry)
        identifier = fields.get("id")
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("catalog entry carries no model id")
        identifiers.add(identifier)
    return sorted(identifiers)


def read_committed_model_ids(path: Path) -> set[str]:
    """Read the model ids of the committed snapshot, or an empty set."""
    if not path.exists():
        return set()
    document: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("committed snapshot is not an object")
    payload = cast("dict[str, object]", document)
    models = payload.get("models")
    if not isinstance(models, list):
        raise ValueError("committed snapshot carries no models array")
    identifiers: set[str] = set()
    for model in cast("list[object]", models):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("committed snapshot carries an invalid model id")
        identifiers.add(model)
    return identifiers


def build_snapshot(model_ids: list[str], retrieved_on: str) -> dict[str, object]:
    """Build the snapshot document written to disk."""
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "provider": PROVIDER_ID,
        "source_url": CATALOG_URL,
        "retrieved_on": retrieved_on,
        "credential_used": False,
        "model_count": len(model_ids),
        "models": model_ids,
    }


def assert_snapshot_target_is_review_evidence(path: Path) -> None:
    """Fail closed if the snapshot target could ever be a canonical asset.

    The refresh writes one file. Guarding the target keeps the "the canonical
    profile asset is the single editable source of provider facts" invariant
    executable rather than aspirational.
    """
    if path.parent != REVIEW_DIRECTORY:
        raise RuntimeError(f"snapshot target {path} is outside the review directory")
    if path.name in CANONICAL_ASSET_NAMES:
        raise RuntimeError(f"snapshot target {path} names a canonical asset")


def write_snapshot_atomically(path: Path, snapshot: dict[str, object]) -> None:
    """Write the snapshot through a temporary file and one rename.

    A concurrent refresh writes its own temporary file, so the published
    snapshot is always one complete document written by one run.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary_path = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def today_utc() -> str:
    """Return today's UTC date as an ISO-8601 calendar date."""
    return datetime.now(UTC).date().isoformat()


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Refresh the dated OpenCode Go catalog snapshot.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the delta without writing the snapshot",
    )
    parser.add_argument(
        "--retrieved-on",
        default=None,
        metavar="YYYY-MM-DD",
        help="override the retrieval date stamp",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Refresh the snapshot and report the delta it introduces."""
    args = parse_args(argv)
    assert_snapshot_target_is_review_evidence(SNAPSHOT_PATH)

    try:
        model_ids = fetch_model_ids(CATALOG_URL)
        previous = read_committed_model_ids(SNAPSHOT_PATH)
    except (RuntimeError, ValueError) as error:
        print(f"catalog refresh failed: {error}", file=sys.stderr)
        return 1

    retrieved_on = args.retrieved_on or today_utc()
    current = set(model_ids)

    print("opencode go catalog snapshot refresh")
    print(f"  source        {CATALOG_URL}")
    print(f"  retrieved on  {retrieved_on}")
    print(f"  previous      {len(previous)} models")
    print(f"  current       {len(current)} models")
    added = sorted(current - previous)
    removed = sorted(previous - current)
    print(f"  added         {', '.join(added) if added else '(none)'}")
    print(f"  removed       {', '.join(removed) if removed else '(none)'}")
    print(f"  snapshot      {SNAPSHOT_PATH}")

    if args.dry_run:
        print("  dry run       snapshot left unchanged")
        return 0

    write_snapshot_atomically(SNAPSHOT_PATH, build_snapshot(model_ids, retrieved_on))
    print("  written       snapshot rewritten; review the delta before committing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
