"""Tests for OpenAI → Anthropic streaming SSE translation."""

from __future__ import annotations

import json
from typing import Any

from eggpool.transcoder.streaming import OpenAIToAnthropicStreaming


def _parse_sse_frames(raw: bytes) -> list[dict[str, Any]]:
    """Parse raw SSE output into a list of {event, data} dicts."""
    frames: list[dict[str, Any]] = []
    for block in raw.split(b"\n\n"):
        if not block.strip():
            continue
        event = ""
        data = ""
        for line in block.split(b"\n"):
            if line.startswith(b"event: "):
                event = line[7:].decode()
            elif line.startswith(b"data: "):
                data = line[6:].decode()
        frames.append({"event": event, "data": data})
    return frames


def _openai_chunk(
    *,
    chunk_id: str = "chatcmpl-1",
    model: str = "gpt-4",
    content: str | None = None,
    role: str | None = None,
    finish_reason: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> bytes:
    """Build an OpenAI SSE data frame."""
    delta: dict[str, Any] = {}
    if role is not None:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls

    choice: dict[str, Any] = {
        "index": 0,
        "delta": delta,
        "finish_reason": finish_reason,
    }
    payload: dict[str, Any] = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": 1234567890,
        "model": model,
        "choices": [choice],
    }
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


def _openai_done() -> bytes:
    return b"data: [DONE]\n\n"


def _openai_usage_chunk(
    *,
    chunk_id: str = "chatcmpl-1",
    model: str = "gpt-4",
    usage: dict[str, Any] | None = None,
) -> bytes:
    """Build an OpenAI SSE usage-only frame (no choices)."""
    payload: dict[str, Any] = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": 1234567890,
        "model": model,
        "choices": [],
    }
    if usage is not None:
        payload["usage"] = usage
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


class TestFirstContentChunk:
    def test_first_content_chunk_emits_message_start_and_block(
        self,
    ) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk = _openai_chunk(role="assistant", content="Hello")
        raw = transcoder.feed(chunk)

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        event_types = [f["event"] for f in frames]
        assert "message_start" in event_types
        assert "content_block_start" in event_types
        assert "content_block_delta" in event_types

    def test_message_start_contains_model_and_id(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk = _openai_chunk(
            chunk_id="chatcmpl-42",
            model="gpt-4o",
            role="assistant",
            content="Hi",
        )
        raw = transcoder.feed(chunk)

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        msg_start = next(f for f in frames if f["event"] == "message_start")
        msg_data = json.loads(msg_start["data"])
        assert msg_data["type"] == "message_start"
        assert msg_data["message"]["id"] == "chatcmpl-42"
        assert msg_data["message"]["model"] == "gpt-4o"

    def test_empty_content_produces_no_output(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk = _openai_chunk(role="assistant", content="")
        raw = transcoder.feed(chunk)

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        event_types = [f["event"] for f in frames]
        assert event_types == ["message_start"]

    def test_role_only_chunk_starts_message_without_content_block(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk = _openai_chunk(role="assistant")
        raw = transcoder.feed(chunk)

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        event_types = [f["event"] for f in frames]
        assert event_types == ["message_start"]


class TestSubsequentChunks:
    def test_subsequent_chunks_emit_only_delta(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk1 = _openai_chunk(role="assistant", content="Hi")
        transcoder.feed(chunk1)

        chunk2 = _openai_chunk(content=" there")
        raw = transcoder.feed(chunk2)

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        event_types = [f["event"] for f in frames]
        assert event_types == ["content_block_delta"]

    def test_delta_contains_text(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk1 = _openai_chunk(role="assistant", content="Start")
        transcoder.feed(chunk1)

        chunk2 = _openai_chunk(content=" continuation")
        raw = transcoder.feed(chunk2)

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        delta_data = json.loads(frames[0]["data"])
        assert delta_data["type"] == "content_block_delta"
        assert delta_data["delta"]["type"] == "text_delta"
        assert delta_data["delta"]["text"] == " continuation"


class TestFinishReason:
    def test_finish_reason_emits_stop_sequence(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk1 = _openai_chunk(role="assistant", content="Hi")
        transcoder.feed(chunk1)

        chunk2 = _openai_chunk(finish_reason="stop")
        raw = transcoder.feed(chunk2)
        raw.extend(transcoder.feed(_openai_done()))

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        event_types = [f["event"] for f in frames]
        assert "content_block_stop" in event_types
        assert "message_delta" in event_types
        assert "message_stop" in event_types

    def test_finish_reason_maps_stop_to_end_turn(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk1 = _openai_chunk(role="assistant", content="Hi")
        transcoder.feed(chunk1)

        chunk2 = _openai_chunk(finish_reason="stop")
        raw = transcoder.feed(chunk2)
        raw.extend(transcoder.feed(_openai_done()))

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        msg_delta = next(f for f in frames if f["event"] == "message_delta")
        delta_data = json.loads(msg_delta["data"])
        assert delta_data["delta"]["stop_reason"] == "end_turn"

    def test_finish_reason_maps_length_to_max_tokens(
        self,
    ) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk1 = _openai_chunk(role="assistant", content="Hi")
        transcoder.feed(chunk1)

        chunk2 = _openai_chunk(finish_reason="length")
        raw = transcoder.feed(chunk2)
        raw.extend(transcoder.feed(_openai_done()))

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        msg_delta = next(f for f in frames if f["event"] == "message_delta")
        delta_data = json.loads(msg_delta["data"])
        assert delta_data["delta"]["stop_reason"] == "max_tokens"


class TestUsageChunk:
    def test_usage_chunk_translates(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk1 = _openai_chunk(role="assistant", content="Hi")
        transcoder.feed(chunk1)

        usage_chunk = _openai_usage_chunk(
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            }
        )
        raw = transcoder.feed(usage_chunk)

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        msg_delta = next(f for f in frames if f["event"] == "message_delta")
        delta_data = json.loads(msg_delta["data"])
        assert delta_data["type"] == "message_delta"
        assert "usage" in delta_data
        assert delta_data["usage"]["output_tokens"] == 5

    def test_usage_chunk_before_done_emits_before_message_stop(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        transcoder.feed(_openai_chunk(role="assistant", content="Hi"))
        finish_raw = transcoder.feed(_openai_chunk(finish_reason="stop"))
        usage_raw = transcoder.feed(
            _openai_usage_chunk(
                usage={
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                }
            )
        )
        done_raw = transcoder.feed(_openai_done())

        frames = _parse_sse_frames(b"".join(finish_raw + usage_raw + done_raw))
        event_types = [f["event"] for f in frames]

        assert event_types == [
            "content_block_stop",
            "message_delta",
            "message_stop",
        ]
        delta_data = json.loads(frames[1]["data"])
        assert delta_data["delta"]["stop_reason"] == "end_turn"
        assert delta_data["usage"]["output_tokens"] == 5

    def test_empty_completion_finishes_without_content_block_events(
        self,
    ) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        role_raw = transcoder.feed(_openai_chunk(role="assistant"))
        finish_raw = transcoder.feed(_openai_chunk(finish_reason="stop"))
        done_raw = transcoder.feed(_openai_done())

        frames = _parse_sse_frames(b"".join(role_raw + finish_raw + done_raw))
        event_types = [f["event"] for f in frames]

        assert event_types == [
            "message_start",
            "message_delta",
            "message_stop",
        ]


class TestEmptyStream:
    def test_empty_stream(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        raw = transcoder.flush()

        assert raw == []


class TestMultiChunkConcatenation:
    def test_multi_chunk_concatenation(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk1 = _openai_chunk(role="assistant", content="Hello")
        raw1 = transcoder.feed(chunk1)

        all_delta_text = []
        for raw in [raw1]:
            frames = _parse_sse_frames(b"".join(raw))
            for f in frames:
                if f["event"] == "content_block_delta":
                    d = json.loads(f["data"])
                    all_delta_text.append(d["delta"]["text"])

        parts = [" ", "world", "!"]
        for part in parts:
            raw = transcoder.feed(_openai_chunk(content=part))
            combined = b"".join(raw)
            frames = _parse_sse_frames(combined)
            for f in frames:
                if f["event"] == "content_block_delta":
                    d = json.loads(f["data"])
                    all_delta_text.append(d["delta"]["text"])

        assert "".join(all_delta_text) == "Hello world!"


class TestIdAndModelPreserved:
    def test_id_and_model_preserved(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk = _openai_chunk(
            chunk_id="chatcmpl-99",
            model="gpt-4o-mini",
            role="assistant",
            content="Hi",
        )
        raw = transcoder.feed(chunk)

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        msg_start = next(f for f in frames if f["event"] == "message_start")
        msg_data = json.loads(msg_start["data"])
        assert msg_data["message"]["id"] == "chatcmpl-99"
        assert msg_data["message"]["model"] == "gpt-4o-mini"

        content_block_start = next(
            f for f in frames if f["event"] == "content_block_start"
        )
        cb_data = json.loads(content_block_start["data"])
        assert cb_data["index"] == 0


class TestArbitraryChunkBoundaries:
    def test_arbitrary_chunk_boundaries(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        full_sse = (
            b'data: {"id":"chatcmpl-1","choices":[{"delta":{'
            b'"role":"assistant","content":"Hello"},"index":0,'
            b'"finish_reason":null}],"model":"gpt-4",'
            b'"object":"chat.completion.chunk",'
            b'"created":1234567890}\n\n'
            b'data: {"id":"chatcmpl-1","choices":[{"delta":{'
            b'"content":" world"},"index":0,'
            b'"finish_reason":null}],"model":"gpt-4",'
            b'"object":"chat.completion.chunk",'
            b'"created":1234567890}\n\n'
        )

        split_at = 50
        chunk1 = full_sse[:split_at]
        chunk2 = full_sse[split_at:]

        raw1 = transcoder.feed(chunk1)
        raw2 = transcoder.feed(chunk2)

        all_frames = []
        for raw in [raw1, raw2]:
            combined = b"".join(raw)
            all_frames.extend(_parse_sse_frames(combined))

        event_types = [f["event"] for f in all_frames]
        assert "message_start" in event_types
        assert "content_block_delta" in event_types

        content_frames = [f for f in all_frames if f["event"] == "content_block_delta"]
        assert len(content_frames) >= 1
        d = json.loads(content_frames[0]["data"])
        assert "Hello" in d["delta"]["text"]


class TestSseParserEventNameReset:
    """Event-only frames must not contaminate subsequent data frames."""

    def test_event_only_frame_resets_current_event(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk = b'event: ping\n\ndata: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        frames = _parse_sse_frames(b"".join(transcoder.feed(chunk)))
        assert [frame["event"] for frame in frames] == ["message_start"]

    def test_multiple_event_only_frames_in_a_row(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk = (
            b"event: ping\n\n"
            b"event: ping\n\n"
            b"event: ping\n\n"
            b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        )
        frames = _parse_sse_frames(b"".join(transcoder.feed(chunk)))
        assert [frame["event"] for frame in frames] == ["message_start"]

    def test_data_only_frame_after_reset(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk = b'event: ping\n\ndata: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        transcoder.feed(chunk)
        second_chunk = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        frames = _parse_sse_frames(b"".join(transcoder.feed(second_chunk)))
        assert [frame["event"] for frame in frames] == [
            "content_block_start",
            "content_block_delta",
        ]
        assert json.loads(frames[1]["data"])["delta"]["text"] == "hi"


class TestToolCallStreaming:
    def test_tool_call_id_and_name_announced_buffers_until_finish(
        self,
    ) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        announce_chunk = _openai_chunk(
            role="assistant",
            tool_calls=[
                {
                    "index": 0,
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": ""},
                }
            ],
        )

        raw = transcoder.feed(announce_chunk)

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        event_types = [f["event"] for f in frames]
        assert "content_block_start" not in event_types
        assert "message_start" in event_types

    def test_tool_call_deltas_buffered_across_chunks(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        transcoder.feed(
            _openai_chunk(
                role="assistant",
                tool_calls=[
                    {
                        "index": 0,
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": ""},
                    }
                ],
            )
        )

        for piece in ['{"city"', ": ", '"SF"}']:
            raw = transcoder.feed(
                _openai_chunk(
                    tool_calls=[
                        {
                            "index": 0,
                            "function": {"arguments": piece},
                        }
                    ],
                )
            )
            combined = b"".join(raw)
            frames = _parse_sse_frames(combined)
            event_types = [f["event"] for f in frames]
            assert "content_block_start" not in event_types
            assert "content_block_stop" not in event_types

    def test_finish_reason_tool_calls_emits_anthropic_blocks(self) -> None:
        from eggpool.transcoder.context import TranscodeContext

        context = TranscodeContext(
            request_id="req-test",
            client_protocol="anthropic",
            upstream_protocol="openai",
        )
        transcoder = OpenAIToAnthropicStreaming(transcode_context=context)

        transcoder.feed(
            _openai_chunk(
                role="assistant",
                tool_calls=[
                    {
                        "index": 0,
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": ""},
                    }
                ],
            )
        )
        transcoder.feed(
            _openai_chunk(
                tool_calls=[
                    {"index": 0, "function": {"arguments": '{"city": "SF"}'}},
                ],
            )
        )
        finish_raw = transcoder.feed(_openai_chunk(finish_reason="tool_calls"))
        stop_raw = transcoder.feed(_openai_done())
        flush_raw = transcoder.flush()

        all_raw = finish_raw + stop_raw + flush_raw
        combined = b"".join(all_raw)
        frames = _parse_sse_frames(combined)

        event_types = [f["event"] for f in frames]
        assert "content_block_start" in event_types
        assert "content_block_stop" in event_types
        assert "message_delta" in event_types
        assert "message_stop" in event_types

        block_start = next(f for f in frames if f["event"] == "content_block_start")
        block_data = json.loads(block_start["data"])
        assert block_data["content_block"]["type"] == "tool_use"
        assert block_data["content_block"]["id"].startswith("toolu_")
        assert block_data["content_block"]["name"] == "get_weather"
        assert block_data["content_block"]["input"] == {"city": "SF"}

        msg_delta = next(f for f in frames if f["event"] == "message_delta")
        delta_data = json.loads(msg_delta["data"])
        assert delta_data["delta"]["stop_reason"] == "tool_use"

    def test_text_tool_calls_and_finish_same_chunk_are_all_emitted(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        raw = transcoder.feed(
            _openai_chunk(
                role="assistant",
                content="I will check that.",
                finish_reason="tool_calls",
                tool_calls=[
                    {
                        "index": 0,
                        "id": "call_abc",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "SF"}',
                        },
                    }
                ],
            )
        )

        raw.extend(transcoder.feed(_openai_done()))
        frames = _parse_sse_frames(b"".join(raw))
        block_starts = [
            json.loads(frame["data"])
            for frame in frames
            if frame["event"] == "content_block_start"
        ]
        assert [block["content_block"]["type"] for block in block_starts] == [
            "text",
            "tool_use",
        ]
        # Indices must be sequential and unique: text at 0, tools after it.
        assert [block["index"] for block in block_starts] == [0, 1]
        block_stops = [
            json.loads(frame["data"])
            for frame in frames
            if frame["event"] == "content_block_stop"
        ]
        assert [block["index"] for block in block_stops] == [0, 1]
        assert any(frame["event"] == "message_delta" for frame in frames)

    def test_multiple_tool_calls_parallel_indices(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        transcoder.feed(
            _openai_chunk(
                role="assistant",
                tool_calls=[
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": ""},
                    },
                    {
                        "index": 1,
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "get_time", "arguments": ""},
                    },
                ],
            )
        )
        transcoder.feed(
            _openai_chunk(
                tool_calls=[
                    {"index": 0, "function": {"arguments": '{"city": "SF"}'}},
                    {"index": 1, "function": {"arguments": '{"tz": "PST"}'}},
                ],
            )
        )
        finish_raw = transcoder.feed(_openai_chunk(finish_reason="tool_calls"))
        stop_raw = transcoder.feed(_openai_done())
        flush_raw = transcoder.flush()

        all_raw = finish_raw + stop_raw + flush_raw
        combined = b"".join(all_raw)
        frames = _parse_sse_frames(combined)

        block_starts = [f for f in frames if f["event"] == "content_block_start"]
        assert len(block_starts) == 2
        block_data_0 = json.loads(block_starts[0]["data"])
        block_data_1 = json.loads(block_starts[1]["data"])
        assert block_data_0["index"] == 0
        assert block_data_1["index"] == 1
        assert block_data_0["content_block"]["name"] == "get_weather"
        assert block_data_1["content_block"]["name"] == "get_time"

        block_stops = [f for f in frames if f["event"] == "content_block_stop"]
        assert len(block_stops) == 2

    def test_malformed_tool_arguments_passes_through_as_raw(self) -> None:
        from eggpool.transcoder.context import TranscodeContext

        context = TranscodeContext(
            request_id="req-test",
            client_protocol="anthropic",
            upstream_protocol="openai",
        )
        transcoder = OpenAIToAnthropicStreaming(transcode_context=context)

        transcoder.feed(
            _openai_chunk(
                role="assistant",
                tool_calls=[
                    {
                        "index": 0,
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": ""},
                    }
                ],
            )
        )
        transcoder.feed(
            _openai_chunk(
                tool_calls=[
                    {
                        "index": 0,
                        "function": {"arguments": "not-valid-json{"},
                    }
                ],
            )
        )
        finish_raw = transcoder.feed(_openai_chunk(finish_reason="tool_calls"))
        stop_raw = transcoder.feed(_openai_done())
        flush_raw = transcoder.flush()

        all_raw = finish_raw + stop_raw + flush_raw
        combined = b"".join(all_raw)
        frames = _parse_sse_frames(combined)

        block_start = next(f for f in frames if f["event"] == "content_block_start")
        block_data = json.loads(block_start["data"])
        assert block_data["content_block"]["input"] == {
            "__raw_arguments__": "not-valid-json{"
        }

        malformed_warnings = [
            w
            for w in context.loss_warnings
            if w.get("streaming_transcoder") == "malformed_tool_arguments"
        ]
        assert len(malformed_warnings) == 1

    def test_tool_call_id_registered_in_id_map(self) -> None:
        from eggpool.transcoder.context import TranscodeContext

        context = TranscodeContext(
            request_id="req-test",
            client_protocol="anthropic",
            upstream_protocol="openai",
        )
        transcoder = OpenAIToAnthropicStreaming(transcode_context=context)

        transcoder.feed(
            _openai_chunk(
                role="assistant",
                tool_calls=[
                    {
                        "index": 0,
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": ""},
                    }
                ],
            )
        )
        transcoder.feed(
            _openai_chunk(
                tool_calls=[
                    {"index": 0, "function": {"arguments": "{}"}},
                ],
            )
        )
        transcoder.feed(_openai_chunk(finish_reason="tool_calls"))
        transcoder.feed(_openai_done())
        transcoder.flush()

        upstream_id = context.id_map.to_upstream("call_abc")
        assert upstream_id is not None
        assert upstream_id.startswith("toolu_")
