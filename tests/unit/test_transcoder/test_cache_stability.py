"""Tests for Phase 3 cache-stability primitives."""

from __future__ import annotations

import pytest

from eggpool.transcoder.cache_stability import (
    CACHE_BOUNDARY_ANNOTATION_CAP,
    CacheBoundaryAnnotation,
    CacheBoundaryTracker,
    extract_cache_boundaries,
    extract_cache_control_type,
    extract_provider_visible_prefix,
    stable_dumps,
    stable_hash,
)


class TestExtractCacheControlType:
    def test_returns_type_for_dict(self) -> None:
        assert extract_cache_control_type({"type": "ephemeral"}) == "ephemeral"

    def test_returns_none_for_non_dict(self) -> None:
        assert extract_cache_control_type("ephemeral") is None
        assert extract_cache_control_type(None) is None
        assert extract_cache_control_type(42) is None
        assert extract_cache_control_type(["ephemeral"]) is None

    def test_returns_none_when_type_missing(self) -> None:
        assert extract_cache_control_type({}) is None
        assert extract_cache_control_type({"ttl": "5m"}) is None

    def test_returns_none_for_non_string_type(self) -> None:
        assert extract_cache_control_type({"type": 1}) is None
        assert extract_cache_control_type({"type": None}) is None
        assert extract_cache_control_type({"type": ["ephemeral"]}) is None


class TestExtractCacheBoundaries:
    def test_empty_for_non_dict(self) -> None:
        assert extract_cache_boundaries(None) == []
        assert extract_cache_boundaries("body") == []
        assert extract_cache_boundaries([{"type": "ephemeral"}]) == []

    def test_empty_for_body_without_cache_control(self) -> None:
        body = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
        }
        assert extract_cache_boundaries(body) == []

    def test_finds_system_block_cache_control(self) -> None:
        body = {
            "system": [
                {"type": "text", "text": "You are helpful."},
                {
                    "type": "text",
                    "text": "Be brief.",
                    "cache_control": {"type": "ephemeral"},
                },
            ]
        }
        found = extract_cache_boundaries(body)
        assert found == [("system[1].cache_control", "ephemeral")]

    def test_finds_tool_cache_control(self) -> None:
        body = {
            "tools": [
                {"name": "search", "cache_control": {"type": "ephemeral"}},
                {"name": "lookup"},
            ]
        }
        found = extract_cache_boundaries(body)
        assert found == [("tools[0].cache_control", "ephemeral")]

    def test_finds_message_content_cache_control(self) -> None:
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Long prompt…",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ]
        }
        found = extract_cache_boundaries(body)
        assert found == [
            (
                "messages[0].content[0].cache_control",
                "ephemeral",
            )
        ]

    def test_invalid_shape_yields_none_type(self) -> None:
        body = {"tools": [{"name": "search", "cache_control": {"oops": True}}]}
        found = extract_cache_boundaries(body)
        assert found == [("tools[0].cache_control", None)]

    def test_system_string_is_skipped(self) -> None:
        body = {"system": "You are helpful."}
        assert extract_cache_boundaries(body) == []

    def test_message_string_content_is_skipped(self) -> None:
        body = {"messages": [{"role": "user", "content": "Hi"}]}
        assert extract_cache_boundaries(body) == []


class TestStableDumps:
    def test_sort_keys_is_deterministic(self) -> None:
        a = {"b": 1, "a": 2}
        b = {"a": 2, "b": 1}
        assert stable_dumps(a) == stable_dumps(b)
        assert stable_dumps(a) == '{"a":2,"b":1}'

    def test_unicode_preserved(self) -> None:
        assert stable_dumps({"text": "héllo"}) == '{"text":"héllo"}'

    def test_default_str_serialises_unsupported_types(self) -> None:
        class Foo:
            def __str__(self) -> str:
                return "foo"

        assert stable_dumps({"x": Foo()}) == '{"x":"foo"}'


class TestStableHash:
    def test_same_payload_produces_same_hash(self) -> None:
        a = {"model": "x", "messages": [{"role": "user", "content": "hi"}]}
        b = {"messages": [{"role": "user", "content": "hi"}], "model": "x"}
        assert stable_hash(a) == stable_hash(b)

    def test_different_payload_produces_different_hash(self) -> None:
        a = {"x": 1}
        b = {"x": 2}
        assert stable_hash(a) != stable_hash(b)

    def test_hash_is_64_hex_chars(self) -> None:
        h = stable_hash({"x": 1})
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


class TestExtractProviderVisiblePrefix:
    def test_returns_none_for_non_dict(self) -> None:
        assert extract_provider_visible_prefix(None) is None
        assert extract_provider_visible_prefix("body") is None
        assert extract_provider_visible_prefix([1, 2]) is None

    def test_strips_last_message(self) -> None:
        body = {
            "model": "claude-3",
            "messages": [
                {"role": "user", "content": "First"},
                {"role": "assistant", "content": "Reply"},
                {"role": "user", "content": "Second"},
            ],
        }
        prefix = extract_provider_visible_prefix(body)
        assert prefix is not None
        assert len(prefix["messages"]) == 2
        assert prefix["messages"][-1]["content"] == "Reply"
        assert "stream" not in prefix

    def test_drops_stream_flag(self) -> None:
        body = {
            "stream": True,
            "messages": [{"role": "user", "content": "Hi"}],
        }
        prefix = extract_provider_visible_prefix(body)
        assert prefix is not None
        assert "stream" not in prefix

    def test_preserves_other_top_level_keys(self) -> None:
        body = {
            "model": "claude-3",
            "system": "Be brief.",
            "max_tokens": 256,
            "messages": [{"role": "user", "content": "Hi"}],
        }
        prefix = extract_provider_visible_prefix(body)
        assert prefix is not None
        assert prefix["model"] == "claude-3"
        assert prefix["system"] == "Be brief."
        assert prefix["max_tokens"] == 256

    def test_empty_messages_returns_empty_messages_list(self) -> None:
        body: dict[str, object] = {"messages": []}
        prefix = extract_provider_visible_prefix(body)
        assert prefix is not None
        assert "messages" not in prefix

    def test_non_list_messages_skipped(self) -> None:
        body = {"messages": "oops"}
        prefix = extract_provider_visible_prefix(body)
        assert prefix is not None
        assert "messages" not in prefix


class TestCacheBoundaryAnnotation:
    def test_frozen_cannot_mutate(self) -> None:
        annotation = CacheBoundaryAnnotation(
            kind="preserved",
            source_protocol="openai",
            target_protocol="anthropic",
            source_path="tools[0].cache_control",
            target_path="tools[0].cache_control",
            cache_control_type="ephemeral",
        )
        with pytest.raises((AttributeError, Exception)):  # FrozenInstanceError
            annotation.kind = "dropped"  # type: ignore[misc]

    def test_to_dict_round_trip(self) -> None:
        annotation = CacheBoundaryAnnotation(
            kind="dropped_unsupported_target",
            source_protocol="anthropic",
            target_protocol="openai",
            source_path="tools[0].cache_control",
            target_path=None,
            cache_control_type="ephemeral",
        )
        d = annotation.to_dict()
        assert d["kind"] == "dropped_unsupported_target"
        assert d["source_protocol"] == "anthropic"
        assert d["target_path"] is None


class TestCacheBoundaryTracker:
    def test_record_appends_in_order(self) -> None:
        tracker = CacheBoundaryTracker()
        tracker.record(
            CacheBoundaryAnnotation(
                kind="preserved",
                source_protocol="openai",
                target_protocol="anthropic",
                source_path="tools[0].cache_control",
                target_path="tools[0].cache_control",
                cache_control_type="ephemeral",
            )
        )
        tracker.record(
            CacheBoundaryAnnotation(
                kind="dropped_unsupported_target",
                source_protocol="anthropic",
                target_protocol="openai",
                source_path="messages[0].content[0].cache_control",
                target_path=None,
                cache_control_type="ephemeral",
            )
        )
        assert len(tracker.annotations) == 2
        assert tracker.to_list()[0]["kind"] == "preserved"
        assert tracker.to_list()[1]["kind"] == "dropped_unsupported_target"

    def test_caps_at_64_and_tracks_dropped_count(self) -> None:
        tracker = CacheBoundaryTracker()
        for i in range(CACHE_BOUNDARY_ANNOTATION_CAP + 5):
            tracker.record(
                CacheBoundaryAnnotation(
                    kind="preserved",
                    source_protocol="openai",
                    target_protocol="anthropic",
                    source_path=f"tools[{i}].cache_control",
                    target_path=f"tools[{i}].cache_control",
                    cache_control_type="ephemeral",
                )
            )
        assert len(tracker.annotations) == CACHE_BOUNDARY_ANNOTATION_CAP
        assert tracker.dropped_count == 5

    def test_to_list_serialises_each_annotation(self) -> None:
        tracker = CacheBoundaryTracker()
        tracker.record(
            CacheBoundaryAnnotation(
                kind="preserved",
                source_protocol="openai",
                target_protocol="anthropic",
                source_path="tools[0].cache_control",
                target_path="tools[0].cache_control",
                cache_control_type="ephemeral",
            )
        )
        result = tracker.to_list()
        assert isinstance(result, list)
        assert result[0]["cache_control_type"] == "ephemeral"
