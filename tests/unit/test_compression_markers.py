"""Tests for deterministic compression markers (Phase 5).

Marker format (single line, deterministic, NO timestamp)::

    [EggPool compression: <transform> | segment=<id>
     | lines=<n> | tokens=<n> | sha256=<digest>]

Round-trips with ``parse_marker(build_marker(...))`` and never
contains a timestamp or date pattern.
"""

from __future__ import annotations

import re

import pytest

from eggpool.transcoder.compression.markers import (
    build_marker,
    is_marker_line,
    parse_marker,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"\b20[2-9]\d[-/]\d{2}[-/]\d{2}\b|\dT\d{2}")
_FAKE_DIGEST = "a" * 64


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_round_trip_basic() -> None:
    marker = build_marker(
        "fold_repeated_lines", "s0:volatile_suffix:messages.5", 100, 50, _FAKE_DIGEST
    )
    parsed = parse_marker(marker)
    assert parsed is not None
    assert parsed["transform"] == "fold_repeated_lines"
    assert parsed["segment"] == "s0:volatile_suffix:messages.5"
    assert parsed["lines"] == "100"
    assert parsed["tokens"] == "50"
    assert parsed["digest"] == _FAKE_DIGEST


# ---------------------------------------------------------------------------
# Format invariant
# ---------------------------------------------------------------------------


def test_format_invariant_single_space_around_pipe() -> None:
    marker = build_marker(
        "compact_logs", "s1:stable_prefix:tools.0", 42, 10, _FAKE_DIGEST
    )
    # Must use exactly single-space-around-pipe separators
    assert " | " in marker
    assert "  |" not in marker
    assert "|  " not in marker
    # Must start/end with brackets
    assert marker.startswith("[EggPool compression:")
    assert marker.endswith("]")


def test_format_matches_regex() -> None:
    marker = build_marker(
        "minify_machine_json", "s2:volatile_suffix:seg2", 0, 0, _FAKE_DIGEST
    )
    # Structural check: brackets, pipe-separated fields
    inner = marker[1:-1]  # strip [ and ]
    parts = inner.split(" | ")
    assert len(parts) == 5
    assert parts[0] == "EggPool compression: minify_machine_json"
    assert parts[1] == "segment=s2:volatile_suffix:seg2"
    assert parts[2] == "lines=0"
    assert parts[3] == "tokens=0"
    assert parts[4] == f"sha256={_FAKE_DIGEST}"


# ---------------------------------------------------------------------------
# Digest length
# ---------------------------------------------------------------------------


def test_digest_is_64_hex_chars() -> None:
    digest = "abcdef0123456789" * 4  # 64 hex chars
    marker = build_marker("fold_repeated_lines", "s0:volatile_suffix:x", 1, 1, digest)
    parsed = parse_marker(marker)
    assert parsed is not None
    assert len(parsed["digest"]) == 64
    assert all(c in "0123456789abcdef" for c in parsed["digest"])


# ---------------------------------------------------------------------------
# No timestamp
# ---------------------------------------------------------------------------


def test_no_timestamp_in_marker() -> None:
    marker = build_marker(
        "compact_logs", "s3:volatile_suffix:messages.0", 200, 100, _FAKE_DIGEST
    )
    assert _DATE_RE.search(marker) is None


# ---------------------------------------------------------------------------
# is_marker_line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "[EggPool compression: fold_repeated_lines | segment=s0:vs:m.0"
            " | lines=5 | tokens=2 | sha256=" + _FAKE_DIGEST + "]",
            True,
        ),
        # Missing closing bracket
        (
            "[EggPool compression: fold_repeated_lines | segment=s0:vs:m.0"
            " | lines=5 | tokens=2 | sha256=" + _FAKE_DIGEST,
            False,
        ),
        # Partial match: no brackets
        ("EggPool compression: fold_repeated_lines", False),
        # Partial match: missing args
        ("[EggPool compression]", False),
        # Random text
        ("hello world", False),
        # Empty
        ("", False),
    ],
    ids=[
        "full_marker",
        "missing_closing_bracket",
        "no_brackets",
        "missing_args",
        "random_text",
        "empty",
    ],
)
def test_is_marker_line(text: str, expected: bool) -> None:
    assert is_marker_line(text) is expected


# ---------------------------------------------------------------------------
# parse_marker returns None on malformed input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "description"),
    [
        (
            "[EggPool compression: fold_repeated_lines | segment=s0:vs:m.0",
            "missing_closing",
        ),
        (
            "[EggPool compression: fold_repeated_lines"
            " | lines=5 | tokens=2 | sha256=" + _FAKE_DIGEST + "]",
            "missing_segment",
        ),
        (
            "[EggPool compression: fold_repeated_lines"
            " | segment=s0:vs:m.0 | lines=5 | tokens=2"
            " | sha256=" + _FAKE_DIGEST.upper() + "]",
            "uppercase_sha256",
        ),
        # Space before closing bracket makes the regex fail
        (
            "[EggPool compression: fold_repeated_lines"
            " | segment=s0:vs:m.0 | lines=5 | tokens=2"
            " | sha256=" + _FAKE_DIGEST + " ]",
            "space_before_closing",
        ),
        ("not a marker at all", "plain_text"),
    ],
    ids=[
        "missing_closing",
        "missing_segment",
        "uppercase_sha256",
        "space_before_closing",
        "plain_text",
    ],
)
def test_parse_marker_returns_none(line: str, description: str) -> None:
    assert parse_marker(line) is None


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_determinism_same_args_same_output() -> None:
    args = (
        "compact_stack_traces",
        "s4:volatile_suffix:messages.2",
        50,
        25,
        _FAKE_DIGEST,
    )
    first = build_marker(*args)
    second = build_marker(*args)
    assert first == second
    # Determinism across calls — same object identity not required,
    # but value equality must hold.
    assert parse_marker(first) == parse_marker(second)


# ---------------------------------------------------------------------------
# Long fields
# ---------------------------------------------------------------------------


def test_long_segment_id_round_trips() -> None:
    long_id = "s99:volatile_suffix:messages.42.content_block_0.tool_result.subfield"
    marker = build_marker(
        "fold_repeated_lines", long_id, 1_000_000, 500_000, _FAKE_DIGEST
    )
    parsed = parse_marker(marker)
    assert parsed is not None
    assert parsed["segment"] == long_id
    assert parsed["lines"] == "1000000"
    assert parsed["tokens"] == "500000"


def test_underscore_in_segment_id_round_trips() -> None:
    seg_id = "s0:stable_prefix:system_message_0"
    marker = build_marker("minify_machine_json", seg_id, 10, 5, _FAKE_DIGEST)
    parsed = parse_marker(marker)
    assert parsed is not None
    assert parsed["segment"] == seg_id
