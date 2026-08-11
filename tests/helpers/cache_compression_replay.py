"""Small helpers for enumerating sanitized cache/compression fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "cache_compression"


def default_fixture_root() -> Path:
    """Return the repository's sanitized cache/compression fixture root."""
    return _FIXTURE_ROOT


def load_fixture(name: str, *, root: Path | None = None) -> dict[str, Any]:
    """Load one JSON fixture by its path relative to the fixture root."""
    path = (root or _FIXTURE_ROOT) / f"{name}.json"
    with path.open(encoding="utf-8") as fixture_file:
        value = json.load(fixture_file)
    if not isinstance(value, dict):
        raise TypeError(f"fixture {name!r} is not a JSON object")
    return value


def iter_fixtures(
    *, category: str | None = None, root: Path | None = None
) -> list[dict[str, Any]]:
    """Load all sanitized fixtures, optionally filtered by category."""
    fixture_root = root or _FIXTURE_ROOT
    fixtures: list[dict[str, Any]] = []
    for path in sorted(fixture_root.rglob("*.json")):
        with path.open(encoding="utf-8") as fixture_file:
            value = json.load(fixture_file)
        if not isinstance(value, dict):
            raise TypeError(f"fixture {path} is not a JSON object")
        if category is None or value.get("category") == category:
            fixtures.append(value)
    return fixtures


__all__ = ["default_fixture_root", "iter_fixtures", "load_fixture"]
