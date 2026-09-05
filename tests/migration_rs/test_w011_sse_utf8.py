"""W011 Python-oracle coverage for SSE UTF-8 EOF finalization."""

from __future__ import annotations

import json

from tests.migration_rs.canonical_wire_fixtures import (
    W011_SSE_UTF8_PATH,
    build_w011_sse_utf8_observations,
)


def test_w011_sse_utf8_observations_are_repeatable_and_snapshot_matches() -> None:
    first = build_w011_sse_utf8_observations()
    second = build_w011_sse_utf8_observations()
    assert first == second
    expected = json.loads(W011_SSE_UTF8_PATH.read_text(encoding="utf-8"))
    assert first == expected


def test_w011_fixture_contains_required_utf8_and_downstream_cases() -> None:
    cases = build_w011_sse_utf8_observations()["cases"]
    names = {case["name"] for case in cases}
    assert names >= {
        "valid_2_byte_scalar_lf",
        "valid_3_byte_scalar_crlf",
        "valid_4_byte_scalar_lf",
        "eof_incomplete_2_prefix_1",
        "eof_incomplete_3_prefix_1",
        "eof_incomplete_4_prefix_1",
        "invalid_continuation_after_prefix",
        "invalid_standalone_before_newline",
        "invalid_standalone_before_eof",
        "invalid_data_line",
        "truncated_data_line_after_json_prefix",
        "invalid_comment_before_newline",
        "truncated_comment_at_eof",
    }
    for case in cases:
        assert case["expected"]["frame_count"] == len(case["expected"]["frames"])
        assert "one_byte" in case["feed_modes"]
