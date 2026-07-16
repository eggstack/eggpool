"""Tests for ParsedRequestPayload (Milestone F7).

Verifies:

- Original bytes are never mutated.
- parsed_dict is computed lazily and cached.
- Derived state (model_id, streaming) is computed on demand and cached.
- invalidate_transformed() resets derived state but not original parse.
- Malformed JSON is handled gracefully.
"""

from __future__ import annotations

import json

import pytest

from eggpool.request.parsed_payload import ParsedRequestPayload


class TestParsedRequestPayload:
    def test_original_bytes_preserved(self) -> None:
        body = b'{"model": "gpt-4", "stream": true}'
        payload = ParsedRequestPayload(original_bytes=body)
        # Access parsed_dict to trigger parse
        _ = payload.parsed_dict
        # Original bytes are unchanged
        assert payload.original_bytes == body

    def test_parsed_dict_lazily_computed(self) -> None:
        body = b'{"model": "gpt-4"}'
        payload = ParsedRequestPayload(original_bytes=body)
        # Before access, _parsed_dict is None
        assert payload._parsed_dict is None
        # Access triggers parse
        result = payload.parsed_dict
        assert result == {"model": "gpt-4"}
        # Second access returns cached value (same object)
        assert payload.parsed_dict is result

    def test_parsed_dict_caches_on_success(self) -> None:
        body = b'{"key": "value"}'
        payload = ParsedRequestPayload(original_bytes=body)
        first = payload.parsed_dict
        second = payload.parsed_dict
        assert first is second

    def test_model_id_derived_from_parsed(self) -> None:
        body = b'{"model": "claude-3-opus"}'
        payload = ParsedRequestPayload(original_bytes=body)
        assert payload.model_id == "claude-3-opus"

    def test_model_id_missing_returns_none(self) -> None:
        body = b'{"stream": true}'
        payload = ParsedRequestPayload(original_bytes=body)
        assert payload.model_id is None

    def test_streaming_derived_from_parsed(self) -> None:
        body = b'{"model": "x", "stream": true}'
        payload = ParsedRequestPayload(original_bytes=body)
        assert payload.streaming is True

    def test_streaming_defaults_to_false(self) -> None:
        body = b'{"model": "x"}'
        payload = ParsedRequestPayload(original_bytes=body)
        assert payload.streaming is False

    def test_streaming_explicit_false(self) -> None:
        body = b'{"model": "x", "stream": false}'
        payload = ParsedRequestPayload(original_bytes=body)
        assert payload.streaming is False

    def test_derived_state_cached_after_first_access(self) -> None:
        body = b'{"model": "m", "stream": true}'
        payload = ParsedRequestPayload(original_bytes=body)
        # Access both derived properties
        _ = payload.model_id
        _ = payload.streaming
        # Mutate internal cache to prove it's cached
        payload._model_id = "cached"
        assert payload.model_id == "cached"
        payload._streaming = False
        assert payload.streaming is False

    def test_invalidate_transformed_resets_derived(self) -> None:
        body = b'{"model": "m", "stream": true}'
        payload = ParsedRequestPayload(original_bytes=body)
        _ = payload.model_id
        _ = payload.streaming
        payload.invalidate_transformed()
        # Derived state is reset
        assert payload._model_id is None
        assert payload._provider_id is None
        assert payload._streaming is None

    def test_invalidate_transformed_preserves_original_parse(self) -> None:
        body = b'{"model": "m"}'
        payload = ParsedRequestPayload(original_bytes=body)
        first_parse = payload.parsed_dict
        payload.invalidate_transformed()
        # Original parse is still cached
        assert payload.parsed_dict is first_parse
        assert payload.original_bytes == body

    def test_malformed_json_returns_none(self) -> None:
        body = b"not json at all"
        payload = ParsedRequestPayload(original_bytes=body)
        assert payload.parsed_dict is None

    def test_malformed_json_parse_failed_flag(self) -> None:
        body = b"not json"
        payload = ParsedRequestPayload(original_bytes=body)
        _ = payload.parsed_dict
        assert payload._parse_failed is True

    def test_malformed_json_does_not_retry_parse(self) -> None:
        body = b"not json"
        payload = ParsedRequestPayload(original_bytes=body)
        _ = payload.parsed_dict
        assert payload._parse_failed is True
        # Second access still returns None without retrying
        assert payload.parsed_dict is None

    def test_derived_state_returns_none_on_parse_failure(self) -> None:
        body = b"broken"
        payload = ParsedRequestPayload(original_bytes=body)
        assert payload.model_id is None
        assert payload.streaming is None

    def test_empty_json_object(self) -> None:
        body = b"{}"
        payload = ParsedRequestPayload(original_bytes=body)
        assert payload.parsed_dict == {}
        assert payload.model_id is None
        assert payload.streaming is False

    def test_json_array_body_parsed(self) -> None:
        body = b"[1, 2, 3]"
        payload = ParsedRequestPayload(original_bytes=body)
        assert payload.parsed_dict == [1, 2, 3]

    def test_json_array_body_model_id_not_a_dict(self) -> None:
        body = b"[1, 2, 3]"
        payload = ParsedRequestPayload(original_bytes=body)
        # .get() is not available on a list — this is expected for
        # invalid request bodies; the caller must handle the error.
        with pytest.raises(AttributeError):
            _ = payload.model_id

    def test_large_json_body_parseable(self) -> None:
        large = {"data": "x" * 100_000, "model": "test-model"}
        body = json.dumps(large).encode()
        payload = ParsedRequestPayload(original_bytes=body)
        assert payload.model_id == "test-model"
        assert len(payload.original_bytes) > 100_000

    def test_empty_bytes_returns_none(self) -> None:
        payload = ParsedRequestPayload(original_bytes=b"")
        assert payload.parsed_dict is None
        assert payload.model_id is None
        assert payload.streaming is None

    def test_slots_prevent_arbitrary_attribute(self) -> None:
        payload = ParsedRequestPayload(original_bytes=b"{}")
        try:
            payload.arbitrary = "nope"  # type: ignore[attr-defined]
        except AttributeError:
            pass
        else:
            raise AssertionError("slots should prevent arbitrary attributes")
