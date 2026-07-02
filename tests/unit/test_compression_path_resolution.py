"""Tests for resolve_path and resolve_text_path (segmentation helpers).

These helpers walk into real OpenAI and Anthropic payloads using
content_path tuples and return the leaf value or None.  In addition to
hand-built paths, the file also verifies that paths emitted by
``segment_request()`` always resolve to a concrete string leaf for
compressions-eligible segments.
"""

from __future__ import annotations

from eggpool.transcoder.segmentation import (
    SegmentKind,
    resolve_path,
    resolve_text_path,
    segment_request,
)

# ---------------------------------------------------------------------------
# OpenAI paths
# ---------------------------------------------------------------------------


def test_resolve_path_openai_string_content() -> None:
    """Walk ("messages", 0, "content") into a string-content message."""
    payload = {"messages": [{"role": "user", "content": "hello"}]}
    assert resolve_path(payload, ("messages", 0, "content")) == "hello"


def test_resolve_path_openai_list_text_part() -> None:
    """Walk ("messages", 0, "content", 0, "text") into a list-of-parts."""
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "hello"}],
            }
        ]
    }
    assert resolve_path(payload, ("messages", 0, "content", 0, "text")) == "hello"


def test_resolve_path_openai_tool_message() -> None:
    """Walk ("messages", 1, "content") into a tool-role message."""
    payload = {
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "tool", "content": "tool output"},
        ]
    }
    assert resolve_path(payload, ("messages", 1, "content")) == "tool output"


def test_resolve_path_openai_tools_array() -> None:
    """Walk ("tools", 0) into the tools array."""
    tool_schema = {
        "type": "function",
        "function": {"name": "get_weather", "parameters": {}},
    }
    payload = {"messages": [], "tools": [tool_schema]}
    assert resolve_path(payload, ("tools", 0)) == tool_schema


# ---------------------------------------------------------------------------
# Anthropic paths
# ---------------------------------------------------------------------------


def test_resolve_path_anthropic_top_level_system() -> None:
    """Walk ("system",) into a string system field."""
    payload = {"system": "You are helpful.", "messages": []}
    assert resolve_path(payload, ("system",)) == "You are helpful."


def test_resolve_path_anthropic_system_block() -> None:
    """Walk ("system", 0, "text") into a list-of-blocks system field."""
    payload = {
        "system": [{"type": "text", "text": "You are helpful."}],
        "messages": [],
    }
    assert resolve_path(payload, ("system", 0, "text")) == "You are helpful."


def test_resolve_path_anthropic_message_text_block() -> None:
    """Walk ("messages", 0, "content", 0, "text") into a text block."""
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "hello"}],
            }
        ]
    }
    result = resolve_path(payload, ("messages", 0, "content", 0, "text"))
    assert result == "hello"


def test_resolve_path_anthropic_tool_result_string() -> None:
    """Walk ("messages", 0, "content", 0, "content") into a tool_result
    block whose content is a plain string."""
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "content": "tool output here",
                    }
                ],
            }
        ]
    }
    result = resolve_path(payload, ("messages", 0, "content", 0, "content"))
    assert result == "tool output here"


def test_resolve_path_anthropic_tool_result_nested() -> None:
    """Walk ("messages", 0, "content", 0, "content", 0, "text") into a
    tool_result block whose content is a nested list."""
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "content": [{"type": "text", "text": "nested text"}],
                    }
                ],
            }
        ]
    }
    result = resolve_path(payload, ("messages", 0, "content", 0, "content", 0, "text"))
    assert result == "nested text"


# ---------------------------------------------------------------------------
# Path resolution for paths emitted by segment_request()
# ---------------------------------------------------------------------------


def test_segment_request_anthropic_tool_result_string_path_resolves() -> None:
    """Anthropic string tool_result content path emitted by
    segment_request() resolves to the string leaf."""
    payload = {
        "system": "You are helpful.",
        "messages": [
            {"role": "user", "content": "Run it."},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "tool output here",
                    }
                ],
            },
        ],
    }
    segmentation = segment_request(payload, protocol="anthropic")
    candidate_paths = [
        s.content_path
        for s in segmentation.segments
        if s.compressible_candidate and s.kind is SegmentKind.VOLATILE_SUFFIX
    ]
    assert ("messages", 1, "content", 0, "content") in candidate_paths
    for path in candidate_paths:
        assert resolve_text_path(payload, path) is not None


def test_segment_request_anthropic_tool_result_nested_text_path_resolves() -> None:
    """Anthropic nested tool_result text path emitted by segment_request()
    resolves to the string leaf."""
    payload = {
        "system": "Sys.",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [
                            {"type": "text", "text": "first text"},
                            {"type": "text", "text": "second text"},
                        ],
                    }
                ],
            }
        ],
    }
    segmentation = segment_request(payload, protocol="anthropic")
    candidate_paths = [
        s.content_path
        for s in segmentation.segments
        if s.compressible_candidate and s.kind is SegmentKind.VOLATILE_SUFFIX
    ]
    assert ("messages", 0, "content", 0, "content", 0, "text") in candidate_paths
    assert ("messages", 0, "content", 0, "content", 1, "text") in candidate_paths
    for path in candidate_paths:
        assert resolve_text_path(payload, path) is not None


def test_segment_request_anthropic_tool_result_defensive_content_field() -> None:
    """Defensive support for nested ``content`` field on tool_result
    nested blocks also resolves through segment_request()."""
    payload = {
        "system": "Sys.",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [
                            {"type": "text", "content": "inline content field"},
                        ],
                    }
                ],
            }
        ],
    }
    segmentation = segment_request(payload, protocol="anthropic")
    candidate_paths = [
        s.content_path
        for s in segmentation.segments
        if s.compressible_candidate and s.kind is SegmentKind.VOLATILE_SUFFIX
    ]
    assert (
        "messages",
        0,
        "content",
        0,
        "content",
        0,
        "content",
    ) in candidate_paths
    for path in candidate_paths:
        assert resolve_text_path(payload, path) is not None


def test_segment_request_openai_compressible_paths_resolve() -> None:
    """All compressible-candidate paths emitted by OpenAI segmentation
    resolve to concrete string leaves."""
    payload = {
        "model": "gpt-4",
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "tool", "content": "ERR: timeout\nERR: timeout\nOK\n"},
        ],
    }
    segmentation = segment_request(payload, protocol="openai")
    candidate_paths = [
        s.content_path
        for s in segmentation.segments
        if s.compressible_candidate and s.kind is SegmentKind.VOLATILE_SUFFIX
    ]
    assert ("messages", 1, "content") in candidate_paths
    for path in candidate_paths:
        assert resolve_text_path(payload, path) is not None


# ---------------------------------------------------------------------------
# Error / edge cases
# ---------------------------------------------------------------------------


def test_resolve_path_missing_returns_none() -> None:
    """Invalid paths return None instead of raising."""
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    assert resolve_path(payload, ("messages", 5, "content")) is None
    assert resolve_path(payload, ("nonexistent",)) is None


def test_resolve_text_path_non_string_returns_none() -> None:
    """Walking to a list or dict leaf returns None for text resolution."""
    payload = {
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    }
    # content is a list, not a string
    assert resolve_text_path(payload, ("messages", 0, "content")) is None


def test_resolve_path_into_dict_key() -> None:
    """Walk into a nested dict by string key."""
    payload = {"metadata": {"request_id": "abc-123"}}
    assert resolve_path(payload, ("metadata", "request_id")) == "abc-123"


def test_resolve_text_path_returns_string_leaf() -> None:
    """resolve_text_path returns the string when the leaf is a string."""
    payload = {"messages": [{"role": "user", "content": "hello"}]}
    assert resolve_text_path(payload, ("messages", 0, "content")) == "hello"


def test_resolve_path_non_indexable_returns_none() -> None:
    """Walking past a non-indexable leaf (int, str) returns None."""
    payload = {"count": 42}
    assert resolve_path(payload, ("count", "nested")) is None
