"""Deterministic marker format for compressed regions.

Phase 5 of the cache-preserving deterministic compression roadmap
introduces inline markers that record what transform was applied, which
segment was affected, and a content digest so the original text is
reproducible from the marker alone.

Marker format (single line, deterministic, NO timestamp)::

    [EggPool compression: <transform> | segment=<id>
     | lines=<n> | tokens=<n> | sha256=<digest>]

Rules:

- ``digest`` is the lowercase hex SHA-256 of the original
  (pre-transform) text (UTF-8 bytes).
- ``segment_id`` is the segment id from the segmenter.
- ``transform`` is one of the six transform names.
- Always use the EXACT format above.  Whitespace inside brackets:
  single space around ``|`` separators.
- ``parse_marker`` must round-trip: ``parse_marker(build_marker(...))``
  matches the input arguments.
- ``is_marker_line(line)`` returns True iff the trimmed line starts
  with ``[EggPool compression:`` and ends with ``]``.

Constraints:

- Pure functions; no I/O; deterministic; no timestamps; no random.
"""

from __future__ import annotations

import re

MarkerLine = str

# Valid transform names.
_VALID_TRANSFORMS: frozenset[str] = frozenset(
    {
        "fold_repeated_lines",
        "compact_logs",
        "compact_search_results",
        "elide_base64_blobs",
        "minify_machine_json",
        "compact_stack_traces",
    }
)

# Regex for parsing marker lines.
_MARKER_RE: re.Pattern[str] = re.compile(
    r"^\[EggPool compression:\s*(?P<transform>[a-zA-Z_]+)"
    r"\s*\|\s*segment=(?P<segment>[^\s|]+)"
    r"\s*\|\s*lines=(?P<lines>\d+)"
    r"\s*\|\s*tokens=(?P<tokens>\d+)"
    r"\s*\|\s*sha256=(?P<digest>[0-9a-f]{64})\]$"
)


def build_marker(
    transform: str,
    segment_id: str,
    original_lines: int,
    original_token_estimate: int,
    digest: str,
) -> str:
    """Build a deterministic compression marker line.

    Parameters
    ----------
    transform:
        One of the six transform names.
    segment_id:
        The segment id from the segmenter.
    original_lines:
        Line count of the original text.
    original_token_estimate:
        Token estimate for the original text.
    digest:
        Lowercase hex SHA-256 of the original text.

    Returns
    -------
    str
        A single-line marker string.
    """
    return (
        f"[EggPool compression: {transform}"
        f" | segment={segment_id}"
        f" | lines={original_lines}"
        f" | tokens={original_token_estimate}"
        f" | sha256={digest}]"
    )


def parse_marker(line: str) -> dict[str, str] | None:
    """Parse a marker line into its component fields.

    Returns ``None`` if the line is not a valid marker.  Round-trips
    with :func:`build_marker`.
    """
    match = _MARKER_RE.match(line.strip())
    if match is None:
        return None
    return {
        "transform": match.group("transform"),
        "segment": match.group("segment"),
        "lines": match.group("lines"),
        "tokens": match.group("tokens"),
        "digest": match.group("digest"),
    }


def is_marker_line(line: str) -> bool:
    """Return True iff ``line`` is a valid EggPool compression marker."""
    stripped = line.strip()
    return stripped.startswith("[EggPool compression:") and stripped.endswith("]")


__all__ = [
    "MarkerLine",
    "build_marker",
    "is_marker_line",
    "parse_marker",
]
