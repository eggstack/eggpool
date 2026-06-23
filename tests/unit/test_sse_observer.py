"""Unit tests for IncrementalSSEObserver."""

from __future__ import annotations

import json
from unittest.mock import patch

from eggpool.proxy.sse_observer import IncrementalSSEObserver, SSEFrame

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sse_data(payload: dict[str, object]) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def _sse_data_raw(text: str) -> bytes:
    """Encode a raw SSE data line with trailing blank line."""
    return f"data: {text}\n\n".encode()


def _sse_done() -> bytes:
    return b"data: [DONE]\n\n"


def _sse_event(event_type: str, payload: dict[str, object]) -> bytes:
    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n".encode()


# ---------------------------------------------------------------------------
# Task 7.1: Incremental UTF-8 decoder
# ---------------------------------------------------------------------------


class TestIncrementalUTF8Decoder:
    """Verify multi-byte UTF-8 sequences survive arbitrary chunking."""

    def test_multibyte_split_across_chunks(self) -> None:
        """A 2-byte UTF-8 character split across two chunks decodes correctly."""
        observer = IncrementalSSEObserver(protocol="openai")
        # U+00E9 = e-acute = 2 bytes: 0xC3 0xA9
        char_bytes = "é".encode()
        assert len(char_bytes) == 2

        observer.observe(char_bytes[:1])
        observer.observe(char_bytes[1:])
        observer.flush()

        # No error from incremental decoder
        assert observer.error_count == 0

    def test_four_byte_utf8_split(self) -> None:
        """A 4-byte UTF-8 character split across chunks decodes correctly."""
        observer = IncrementalSSEObserver(protocol="openai")
        # U+1F600 = grinning face = 4 bytes
        char_bytes = "\U0001f600".encode("utf-8")
        assert len(char_bytes) == 4

        for i in range(len(char_bytes)):
            observer.observe(char_bytes[i : i + 1])
        observer.flush()

        assert observer.error_count == 0

    def test_bom_not_added(self) -> None:
        """Incremental decoder does not add BOM."""
        observer = IncrementalSSEObserver(protocol="openai")
        observer.observe(b"hello")
        observer.flush()
        assert observer.error_count == 0

    def test_flush_decoder_remainder(self) -> None:
        """Flush completes any pending multi-byte sequence in the decoder."""
        observer = IncrementalSSEObserver(protocol="openai")
        # Send an incomplete 3-byte sequence
        observer.observe(b"\xc3")  # First byte of 2-byte sequence
        # Flush with final=True
        observer.flush()
        # Should not raise
        assert observer.bytes_emitted == 1


# ---------------------------------------------------------------------------
# Task 7.2: Parse complete SSE events
# ---------------------------------------------------------------------------


class TestSSEEventAssembly:
    """Verify correct assembly of complete SSE events."""

    def test_single_data_line(self) -> None:
        """Single data line produces one event."""
        observer = IncrementalSSEObserver(protocol="openai")
        payload = {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        observer.observe(_sse_data(payload))
        observer.flush()

        assert observer.usage.input_tokens == 10
        assert observer.usage.output_tokens == 5

    def test_multiple_data_lines_joined(self) -> None:
        """Multiple data: lines before blank line are joined with \\n."""
        observer = IncrementalSSEObserver(protocol="openai")
        raw = b'data: {"usage": {"prompt_tokens": 10,\n'
        raw += b'data:  "completion_tokens": 5}}\n\n'
        observer.observe(raw)
        observer.flush()

        assert observer.usage.input_tokens == 10
        assert observer.usage.output_tokens == 5

    def test_data_value_without_space(self) -> None:
        """data:value (no space after colon) is accepted."""
        observer = IncrementalSSEObserver(protocol="openai")
        payload = {"usage": {"prompt_tokens": 7}}
        raw = f"data:{json.dumps(payload)}\n\n".encode()
        observer.observe(raw)
        observer.flush()

        assert observer.usage.input_tokens == 7

    def test_data_value_with_space(self) -> None:
        """data: value (space after colon) is accepted."""
        observer = IncrementalSSEObserver(protocol="openai")
        payload = {"usage": {"prompt_tokens": 12}}
        observer.observe(_sse_data(payload))
        observer.flush()

        assert observer.usage.input_tokens == 12

    def test_comments_ignored(self) -> None:
        """Lines beginning with : are comments and ignored."""
        observer = IncrementalSSEObserver(protocol="openai")
        raw = b": this is a comment\ndata: [DONE]\n\n"
        observer.observe(raw)
        observer.flush()

        assert observer.error_count == 0
        assert observer.usage.input_tokens == 0

    def test_unknown_fields_ignored(self) -> None:
        """Unknown fields (event:, id:, retry:) are ignored."""
        observer = IncrementalSSEObserver(protocol="openai")
        payload = {"usage": {"prompt_tokens": 3}}
        raw = b"event: test\nid: 123\nretry: 5000\n"
        raw += f"data: {json.dumps(payload)}\n\n".encode()
        observer.observe(raw)
        observer.flush()

        assert observer.usage.input_tokens == 3

    def test_done_terminates_event(self) -> None:
        """[DONE] is recognized but not an error."""
        observer = IncrementalSSEObserver(protocol="openai")
        observer.observe(b"data: [DONE]\n\n")
        observer.flush()

        assert observer.error_count == 0
        assert observer.usage.input_tokens == 0

    def test_blank_line_separates_events(self) -> None:
        """Blank line separates two SSE events."""
        observer = IncrementalSSEObserver(protocol="openai")
        p1 = {"usage": {"prompt_tokens": 10}}
        p2 = {"usage": {"completion_tokens": 20}}
        raw = _sse_data(p1) + _sse_data(p2)
        observer.observe(raw)
        observer.flush()

        assert observer.usage.input_tokens == 10
        assert observer.usage.output_tokens == 20

    def test_crlf_normalized(self) -> None:
        """CRLF is normalized to LF before processing."""
        observer = IncrementalSSEObserver(protocol="openai")
        payload = {"usage": {"prompt_tokens": 5}}
        raw = f"data: {json.dumps(payload)}\r\n\r\n".encode()
        observer.observe(raw)
        observer.flush()

        assert observer.usage.input_tokens == 5

    def test_lone_cr_normalized(self) -> None:
        """Lone CR is normalized to LF before processing."""
        observer = IncrementalSSEObserver(protocol="openai")
        payload = {"usage": {"prompt_tokens": 5}}
        raw = f"data: {json.dumps(payload)}\r\r".encode()
        observer.observe(raw)
        observer.flush()

        assert observer.usage.input_tokens == 5

    def test_anthropic_message_start(self) -> None:
        """Anthropic message_start event extracts input tokens."""
        observer = IncrementalSSEObserver(protocol="anthropic")
        event = {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 42}},
        }
        observer.observe(_sse_event("message_start", event))
        observer.flush()

        assert observer.usage.input_tokens == 42

    def test_anthropic_message_delta(self) -> None:
        """Anthropic message_delta event extracts output tokens."""
        observer = IncrementalSSEObserver(protocol="anthropic")
        event = {
            "type": "message_delta",
            "usage": {"output_tokens": 15},
        }
        observer.observe(_sse_event("message_delta", event))
        observer.flush()

        assert observer.usage.output_tokens == 15
        assert observer.usage.is_complete

    def test_anthropic_content_block_delta_thinking(self) -> None:
        """Anthropic content_block_delta thinking event extracts characters."""
        observer = IncrementalSSEObserver(protocol="anthropic")
        event = {
            "type": "content_block_delta",
            "delta": {"type": "thinking", "thinking": "hello world"},
        }
        observer.observe(_sse_event("content_block_delta", event))
        observer.flush()

        assert observer.usage.thinking_characters == 11

    def test_malformed_json_increments_error(self) -> None:
        """Malformed JSON in a data line increments error count."""
        observer = IncrementalSSEObserver(protocol="openai")
        observer.observe(b"data: {not valid json}\n\n")
        observer.flush()

        assert observer.error_count == 1

    def test_openai_content_chunks_skip_json_decoding(self) -> None:
        """Only the final usage chunk is decoded for OpenAI telemetry."""
        observer = IncrementalSSEObserver(protocol="openai")
        content = _sse_data({"choices": [{"delta": {"content": "token"}}]})
        usage = _sse_data({"choices": [], "usage": {"prompt_tokens": 10}})

        with patch("eggpool.proxy.sse_observer.json.loads", wraps=json.loads) as loads:
            observer.observe(content * 100 + usage)
            observer.flush()

        assert observer.usage.input_tokens == 10
        assert loads.call_count == 1


# ---------------------------------------------------------------------------
# Task 7.3: Bound memory
# ---------------------------------------------------------------------------


class TestMemoryBounds:
    """Verify memory is bounded for incomplete frames."""

    def test_incomplete_event_exceeding_limit(self) -> None:
        """An incomplete event exceeding MAX_INCOMPLETE_FRAME_BYTES is discarded."""
        from eggpool.proxy.sse_observer import MAX_INCOMPLETE_FRAME_BYTES

        observer = IncrementalSSEObserver(protocol="openai")
        # Send a huge data line without a blank line terminator
        huge_data = "x" * (MAX_INCOMPLETE_FRAME_BYTES + 1000)
        observer.observe(f"data: {huge_data}".encode())

        # The partial line is discarded until its delimiter arrives.
        assert observer._buffer == ""
        assert observer.error_count >= 1

        observer.observe(b"\n\n" + _sse_data({"usage": {"prompt_tokens": 11}}))
        observer.flush()

        assert observer.usage.input_tokens == 11

    def test_incomplete_event_limit_counts_utf8_bytes(self) -> None:
        """The partial-line limit is a byte limit for non-ASCII input too."""
        from eggpool.proxy.sse_observer import MAX_INCOMPLETE_FRAME_BYTES

        observer = IncrementalSSEObserver(protocol="openai")
        observer.observe(("data: " + "😀" * MAX_INCOMPLETE_FRAME_BYTES).encode())

        assert observer._buffer == ""
        assert observer.error_count == 1

    def test_multiline_event_is_bounded_and_observer_recovers(self) -> None:
        """Complete data lines cannot accumulate an unbounded unterminated event."""
        from eggpool.proxy.sse_observer import MAX_INCOMPLETE_FRAME_BYTES

        observer = IncrementalSSEObserver(protocol="openai")
        line = b"data: " + b"x" * (MAX_INCOMPLETE_FRAME_BYTES // 2) + b"\n"

        observer.observe(line + line + b"\n")
        observer.observe(_sse_data({"usage": {"prompt_tokens": 17}}))
        observer.flush()

        assert observer.error_count == 1
        assert observer.usage.input_tokens == 17

    def test_no_error_for_normal_stream(self) -> None:
        """Normal SSE stream does not trigger memory bounds."""
        observer = IncrementalSSEObserver(protocol="openai")
        for i in range(100):
            payload = {"index": i}
            observer.observe(_sse_data(payload))
        observer.flush()

        assert observer.error_count == 0


# ---------------------------------------------------------------------------
# Task 7.4: Byte counting and first byte tracking
# ---------------------------------------------------------------------------


class TestByteTracking:
    """Verify bytes_emitted is tracked correctly."""

    def test_bytes_emitted_matches_input(self) -> None:
        """bytes_emitted equals total bytes passed to observe()."""
        observer = IncrementalSSEObserver(protocol="openai")
        data = b"hello world"
        observer.observe(data)
        assert observer.bytes_emitted == len(data)

    def test_bytes_emitted_accumulates(self) -> None:
        """bytes_emitted accumulates across multiple observe() calls."""
        observer = IncrementalSSEObserver(protocol="openai")
        observer.observe(b"aaa")
        observer.observe(b"bbb")
        observer.observe(b"ccc")
        assert observer.bytes_emitted == 9

    def test_frame_count_increments(self) -> None:
        """frame_count increments for each SSE line processed."""
        observer = IncrementalSSEObserver(protocol="openai")
        observer.observe(b"data: {}\nevent: test\n: comment\n")
        observer.flush()
        # data line + event line + comment line = 3 lines
        assert observer.frame_count == 3


# ---------------------------------------------------------------------------
# Split at every byte boundary
# ---------------------------------------------------------------------------


class TestArbitraryChunking:
    """Verify identical usage extraction regardless of chunk boundaries."""

    def _make_sse_stream(self) -> bytes:
        """Build a complete SSE stream with usage."""
        chunks = []
        p1 = {"usage": {"prompt_tokens": 100, "completion_tokens": 50}}
        chunks.append(f"data: {json.dumps(p1)}\n\n".encode())
        p2 = {
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "completion_tokens_details": {"reasoning_tokens": 25},
            }
        }
        chunks.append(f"data: {json.dumps(p2)}\n\n".encode())
        chunks.append(b"data: [DONE]\n\n")
        return b"".join(chunks)

    def test_split_at_every_byte_boundary(self) -> None:
        """Usage extraction is identical for every possible byte-level split."""
        full_stream = self._make_sse_stream()

        expected = IncrementalSSEObserver(protocol="openai")
        expected.observe(full_stream)
        expected.flush()

        for split_point in range(1, len(full_stream)):
            observer = IncrementalSSEObserver(protocol="openai")
            observer.observe(full_stream[:split_point])
            observer.observe(full_stream[split_point:])
            observer.flush()

            assert observer.usage.input_tokens == expected.usage.input_tokens, (
                f"Split at {split_point}: input_tokens mismatch"
            )
            assert observer.usage.output_tokens == expected.usage.output_tokens, (
                f"Split at {split_point}: output_tokens mismatch"
            )
            assert observer.usage.reasoning_tokens == expected.usage.reasoning_tokens, (
                f"Split at {split_point}: reasoning_tokens mismatch"
            )
            assert observer.bytes_emitted == expected.bytes_emitted, (
                f"Split at {split_point}: bytes_emitted mismatch"
            )

    def test_split_inside_multibyte_character(self) -> None:
        """Splitting inside a multi-byte UTF-8 character in JSON works."""
        payload2 = {"model": "gpt-4", "text": "café résumé"}
        data_line2 = f"data: {json.dumps(payload2)}\n\n"
        full_stream2 = data_line2.encode("utf-8")

        # Verify that splitting at any point in the second stream
        # doesn't cause errors
        for split_point in range(1, len(full_stream2)):
            observer = IncrementalSSEObserver(protocol="openai")
            observer.observe(full_stream2[:split_point])
            observer.observe(full_stream2[split_point:])
            observer.flush()
            # No assertion on usage - just verify no crash

    def test_split_between_data_colon_and_payload(self) -> None:
        """Splitting between 'data: ' and the JSON payload works."""
        payload = {"usage": {"prompt_tokens": 42}}
        full_stream = f"data: {json.dumps(payload)}\n\n".encode()

        for split_point in range(1, len(full_stream)):
            observer = IncrementalSSEObserver(protocol="openai")
            observer.observe(full_stream[:split_point])
            observer.observe(full_stream[split_point:])
            observer.flush()

            assert observer.usage.input_tokens == 42, (
                f"Split at {split_point}: input_tokens should be 42"
            )

    def test_split_between_cr_and_lf(self) -> None:
        """Splitting between \\r and \\n in CRLF works."""
        payload = {"usage": {"prompt_tokens": 99}}
        full_stream = f"data: {json.dumps(payload)}\r\n\r\n".encode()

        for split_point in range(1, len(full_stream)):
            observer = IncrementalSSEObserver(protocol="openai")
            observer.observe(full_stream[:split_point])
            observer.observe(full_stream[split_point:])
            observer.flush()

            assert observer.usage.input_tokens == 99, (
                f"Split at {split_point}: input_tokens should be 99"
            )

    def test_split_crlf_preserves_multiline_event(self) -> None:
        """A split CRLF must not become a false event separator."""
        raw = (
            b'data: {"usage": {"prompt_tokens": 10,\r\n'
            b'data: "completion_tokens": 20}}\r\n\r\n'
        )
        split_point = raw.index(b"\r\n") + 1
        observer = IncrementalSSEObserver(protocol="openai")

        observer.observe(raw[:split_point])
        observer.observe(raw[split_point:])
        observer.flush()

        assert observer.usage.input_tokens == 10
        assert observer.usage.output_tokens == 20

    def test_invalid_utf8_does_not_break_stream_observation(self) -> None:
        """Malformed telemetry bytes are ignored instead of raising."""
        observer = IncrementalSSEObserver(protocol="openai")

        observer.observe(b"data: \xff\xfe\n\n")
        observer.flush()

        assert observer.error_count == 1

    def test_split_across_blank_line_boundary(self) -> None:
        """Splitting across a blank line event boundary works."""
        p1 = {"usage": {"prompt_tokens": 10}}
        p2 = {"usage": {"prompt_tokens": 20}}
        full_stream = _sse_data(p1) + _sse_data(p2)

        for split_point in range(1, len(full_stream)):
            observer = IncrementalSSEObserver(protocol="openai")
            observer.observe(full_stream[:split_point])
            observer.observe(full_stream[split_point:])
            observer.flush()

            assert observer.usage.input_tokens == 30, (
                f"Split at {split_point}: input_tokens should be 30"
            )

    def test_split_across_multiline_data(self) -> None:
        """Splitting across multi-line data: lines works."""
        raw = (
            b'data: {"usage": {"prompt_tokens": 10,\n'
            b'data:  "completion_tokens": 20}}\n\n'
        )

        for split_point in range(1, len(raw)):
            observer = IncrementalSSEObserver(protocol="openai")
            observer.observe(raw[:split_point])
            observer.observe(raw[split_point:])
            observer.flush()

            assert observer.usage.input_tokens == 10, (
                f"Split at {split_point}: input_tokens should be 10"
            )
            assert observer.usage.output_tokens == 20, (
                f"Split at {split_point}: output_tokens should be 20"
            )

    def test_exact_downstream_bytes_match(self) -> None:
        """Downstream bytes equal upstream bytes for all splits."""
        full_stream = self._make_sse_stream()

        for split_point in range(1, len(full_stream)):
            observer = IncrementalSSEObserver(protocol="openai")
            observer.observe(full_stream[:split_point])
            observer.observe(full_stream[split_point:])
            observer.flush()

            assert observer.bytes_emitted == len(full_stream), (
                f"Split at {split_point}: bytes_emitted mismatch"
            )


# ---------------------------------------------------------------------------
# SSEFrame helper
# ---------------------------------------------------------------------------


class TestSSEFrame:
    """Verify SSEFrame helper dataclass."""

    def test_data_property(self) -> None:
        frame = SSEFrame(data_lines=["hello", "world"])
        assert frame.data == "hello\nworld"

    def test_is_done(self) -> None:
        frame = SSEFrame(data_lines=["[DONE]"])
        assert frame.is_done

    def test_is_not_done(self) -> None:
        frame = SSEFrame(data_lines=["{not done}"])
        assert not frame.is_done
