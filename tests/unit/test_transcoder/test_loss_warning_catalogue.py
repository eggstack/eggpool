"""Sync test: LOSS_WARNING_KINDS stays in sync with emitted warning kinds.

The plan (§ Validation per sub-phase) requires a unit test asserting the
catalogue stays in sync with actual warning strings emitted by the
translators.
"""

from __future__ import annotations

import re
from pathlib import Path

from eggpool.transcoder import LOSS_WARNING_KINDS

# Source directories to scan for emitted warning kinds.
_SRC_DIRS = [
    Path(__file__).resolve().parents[3] / "src" / "eggpool" / "transcoder",
]

# Match "kind": "some_kind" on the same line OR across lines (multiline dicts).
_KIND_RE = re.compile(r'"kind":\s*\n?\s*"([a-z_]+)"')
# Match self._warn("some_kind_name", ...) in the streaming transcoder.
# The streaming transcoder uses {"streaming_transcoder": message} as its
# warning shape instead of {"kind": ...}.
_WARN_CALL_RE = re.compile(r'self\._warn\(\s*"([a-z_]+)"')


def _collect_emitted_kinds() -> set[str]:
    """Scan transcoder source files for all emitted warning kind values."""
    kinds: set[str] = set()
    for src_dir in _SRC_DIRS:
        if not src_dir.is_dir():
            continue
        for py_file in src_dir.rglob("*.py"):
            text = py_file.read_text()
            for match in _KIND_RE.finditer(text):
                kinds.add(match.group(1))
            for match in _WARN_CALL_RE.finditer(text):
                kinds.add(match.group(1))
    return kinds


def test_emitted_kinds_are_registered() -> None:
    """Every warning kind emitted in source code must be in LOSS_WARNING_KINDS."""
    emitted = _collect_emitted_kinds()
    unregistered = emitted - LOSS_WARNING_KINDS
    assert not unregistered, (
        f"Warning kinds emitted in source but not in LOSS_WARNING_KINDS: "
        f"{sorted(unregistered)}"
    )


def test_registered_kinds_are_emitted() -> None:
    """Every kind in LOSS_WARNING_KINDS should be emitted somewhere.

    This catches dead entries that were registered but never used, which
    indicates either a missing implementation or a stale registration.
    """
    emitted = _collect_emitted_kinds()
    unused = LOSS_WARNING_KINDS - emitted
    assert not unused, (
        f"LOSS_WARNING_KINDS entries never emitted in source code: {sorted(unused)}"
    )
