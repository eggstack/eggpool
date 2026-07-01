"""Tests for Anthropic → OpenAI streaming SSE translation."""

from __future__ import annotations

import json
from typing import Any

import pytest

from eggpool.transcoder.streaming import AnthropicToOpenAIStreaming


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
        if data == "[DONE]":
            frames.append({"event": event, "data": data, "done": True})
            continue
        frames.append({"event": event, "data": data})
    return frames


def _anthropic_sse(
    event: str,
    *,
    event_type: str | None = None,
    message_id: str = "msg-1",
    model: str = "claude-3",
    content: str | None = None,
    stop_reason: str | None = None,
    usage: dict[str, Any] | None = None,
    index: int = 0,
    tool_id: str | None = None,
    tool_name: str | None = None,
    partial_json: str | None = None,
    block_type: str = "text",
) -> bytes:
    """Build an Anthropic SSE frame."""
    if event_type is None:
        event_type = event

    if event == "message_start":
        payload: dict[str, Any] = {
            "type": event_type,
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "usage": usage or {"input_tokens": 10, "output_tokens": 0},
            },
        }
    elif event == "content_block_start":
        if block_type == "tool_use":
            content_block: dict[str, Any] = {
                "type": "tool_use",
                "id": tool_id or "toolu_default",
                "name": tool_name or "default_tool",
                "input": {},
            }
        else:
            content_block = {"type": "text", "text": ""}
        payload = {
            "type": event_type,
            "index": index,
            "content_block": content_block,
        }
    elif event == "content_block_delta":
        if partial_json is not None:
            delta_obj: dict[str, Any] = {
                "type": "input_json_delta",
                "partial_json": partial_json,
            }
        else:
            delta_obj = {"type": "text_delta", "text": content or ""}
        payload = {
            "type": event_type,
            "index": index,
            "delta": delta_obj,
        }
    elif event == "content_block_stop":
        payload = {"type": event_type, "index": index}
    elif event == "message_delta":
        payload = {
            "type": event_type,
            "delta": {
                "stop_reason": stop_reason,
                "stop_sequence": None,
            },
            "usage": usage or {"output_tokens": 5},
        }
    elif event == "message_stop":
        payload = {"type": event_type}
    else:
        payload = {"type": event_type}

    return (
        b"event: " + event.encode() + b"\n"
        b"data: " + json.dumps(payload).encode() + b"\n\n"
    )


class TestMessageStart:
    @pytest.mark.asyncio
    async def test_message_start_emits_role_delta(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        raw = await transcoder.feed(
            _anthropic_sse(
                "message_start",
                message_id="msg-1",
                model="claude-3",
            )
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        assert len(frames) == 1
        data = json.loads(frames[0]["data"])
        assert data["choices"][0]["delta"]["role"] == "assistant"
        assert data["choices"][0]["delta"]["content"] == ""

    @pytest.mark.asyncio
    async def test_message_start_preserves_id(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        raw = await transcoder.feed(
            _anthropic_sse("message_start", message_id="msg-xyz")
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        data = json.loads(frames[0]["data"])
        assert data["id"] == "msg-xyz"


class TestContentBlockStart:
    @pytest.mark.asyncio
    async def test_content_block_start_noop(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(
            _anthropic_sse(
                "message_start",
                message_id="msg-1",
                model="claude-3",
            )
        )

        raw = await transcoder.feed(_anthropic_sse("content_block_start", index=0))

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        assert len(frames) == 0


class TestContentBlockDelta:
    @pytest.mark.asyncio
    async def test_content_block_delta_emits_content(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(
            _anthropic_sse(
                "message_start",
                message_id="msg-1",
                model="claude-3",
            )
        )
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))

        raw = await transcoder.feed(
            _anthropic_sse("content_block_delta", content="Hello", index=0)
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        assert len(frames) == 1
        data = json.loads(frames[0]["data"])
        assert data["choices"][0]["delta"]["content"] == "Hello"
        assert data["choices"][0]["delta"].get("role") is None

    @pytest.mark.asyncio
    async def test_multiple_deltas_concatenate(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(
            _anthropic_sse(
                "message_start",
                message_id="msg-1",
                model="claude-3",
            )
        )
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))

        texts = ["Hello", " ", "world"]
        all_content = []
        for text in texts:
            raw = await transcoder.feed(
                _anthropic_sse("content_block_delta", content=text, index=0)
            )
            combined = b"".join(raw)
            frames = _parse_sse_frames(combined)
            for f in frames:
                data = json.loads(f["data"])
                all_content.append(data["choices"][0]["delta"]["content"])

        assert "".join(all_content) == "Hello world"


class TestMessageDelta:
    @pytest.mark.asyncio
    async def test_message_delta_emits_finish_and_done(self) -> None:
        """message_delta with end_turn emits finish + [DONE]."""
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(
            _anthropic_sse(
                "message_start",
                message_id="msg-1",
                model="claude-3",
            )
        )
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))
        await transcoder.feed(
            _anthropic_sse("content_block_delta", content="Hi", index=0)
        )
        await transcoder.feed(_anthropic_sse("content_block_stop", index=0))

        raw = await transcoder.feed(
            _anthropic_sse(
                "message_delta",
                stop_reason="end_turn",
                usage={"output_tokens": 5},
            )
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        has_done = any(f["data"] == "[DONE]" for f in frames)
        assert has_done

        finish_frames = [
            f
            for f in frames
            if f["data"] != "[DONE]" and f["data"] and json.loads(f["data"])["choices"]
        ]
        assert len(finish_frames) == 1
        finish_data = json.loads(finish_frames[0]["data"])
        assert finish_data["choices"][0]["finish_reason"] == "stop"

    @pytest.mark.asyncio
    async def test_flush_after_message_delta_does_not_duplicate_done(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(
            _anthropic_sse(
                "message_start",
                message_id="msg-1",
                model="claude-3",
            )
        )

        raw = await transcoder.feed(
            _anthropic_sse("message_delta", stop_reason="end_turn")
        )
        raw.extend(await transcoder.flush())

        frames = _parse_sse_frames(b"".join(raw))
        done_frames = [f for f in frames if f["data"] == "[DONE]"]
        assert len(done_frames) == 1

    @pytest.mark.asyncio
    async def test_message_delta_maps_max_tokens(self) -> None:
        """message_delta with max_tokens emits finish_reason chunk."""
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(
            _anthropic_sse(
                "message_start",
                message_id="msg-1",
                model="claude-3",
            )
        )
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))
        await transcoder.feed(
            _anthropic_sse("content_block_delta", content="Hi", index=0)
        )
        await transcoder.feed(_anthropic_sse("content_block_stop", index=0))

        raw = await transcoder.feed(
            _anthropic_sse("message_delta", stop_reason="max_tokens")
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        finish_frames = [
            f
            for f in frames
            if f["data"] != "[DONE]" and f["data"] and json.loads(f["data"])["choices"]
        ]
        assert len(finish_frames) == 1
        finish_data = json.loads(finish_frames[0]["data"])
        assert finish_data["choices"][0]["finish_reason"] == "length"

    @pytest.mark.asyncio
    async def test_message_delta_tool_use_maps_to_tool_calls(
        self,
    ) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(
            _anthropic_sse(
                "message_start",
                message_id="msg-1",
                model="claude-3",
            )
        )
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))
        await transcoder.feed(
            _anthropic_sse("content_block_delta", content="Hi", index=0)
        )
        await transcoder.feed(_anthropic_sse("content_block_stop", index=0))

        raw = await transcoder.feed(
            _anthropic_sse("message_delta", stop_reason="tool_use")
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        finish_frames = [
            f
            for f in frames
            if f["data"] != "[DONE]" and f["data"] and json.loads(f["data"])["choices"]
        ]
        assert len(finish_frames) == 1
        finish_data = json.loads(finish_frames[0]["data"])
        assert finish_data["choices"][0]["finish_reason"] == "tool_calls"


class TestUsageInMessageDelta:
    @pytest.mark.asyncio
    async def test_usage_in_message_delta(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(
            _anthropic_sse(
                "message_start",
                message_id="msg-1",
                model="claude-3",
            )
        )
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))
        await transcoder.feed(
            _anthropic_sse("content_block_delta", content="Hi", index=0)
        )
        await transcoder.feed(_anthropic_sse("content_block_stop", index=0))

        raw = await transcoder.feed(
            _anthropic_sse(
                "message_delta",
                stop_reason="end_turn",
                usage={"output_tokens": 7},
            )
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        finish_frames = [
            f
            for f in frames
            if f["data"] != "[DONE]" and f["data"] and json.loads(f["data"])["choices"]
        ]
        assert len(finish_frames) == 1
        finish_data = json.loads(finish_frames[0]["data"])
        assert "usage" in finish_data
        assert finish_data["usage"]["completion_tokens"] == 7

    @pytest.mark.asyncio
    async def test_usage_includes_cache_tokens_in_prompt_total(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(
            _anthropic_sse(
                "message_start",
                message_id="msg-1",
                model="claude-3",
                usage={
                    "input_tokens": 850,
                    "cache_read_input_tokens": 75_000,
                    "cache_creation_input_tokens": 4_000,
                },
            )
        )

        raw = await transcoder.feed(
            _anthropic_sse(
                "message_delta",
                stop_reason="end_turn",
                usage={"output_tokens": 25},
            )
        )

        frames = _parse_sse_frames(b"".join(raw))
        finish_frame = next(
            f
            for f in frames
            if f["data"] != "[DONE]" and json.loads(f["data"])["choices"]
        )
        finish_data = json.loads(finish_frame["data"])
        assert finish_data["usage"]["prompt_tokens"] == 79_850
        assert finish_data["usage"]["completion_tokens"] == 25
        assert finish_data["usage"]["total_tokens"] == 79_875
        assert finish_data["usage"]["prompt_tokens_details"] == {
            "cached_tokens": 75_000,
            "cache_creation_tokens": 4_000,
        }


class TestEmptyStream:
    @pytest.mark.asyncio
    async def test_empty_stream(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        raw = await transcoder.flush()

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        assert len(frames) >= 1
        assert frames[-1]["data"] == "[DONE]"


class TestIdAndModelPreserved:
    @pytest.mark.asyncio
    async def test_id_and_model_preserved(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(
            _anthropic_sse(
                "message_start",
                message_id="msg-abc",
                model="claude-3-opus",
            )
        )
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))

        raw = await transcoder.feed(
            _anthropic_sse("content_block_delta", content="Hi", index=0)
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        data = json.loads(frames[0]["data"])
        assert data["id"] == "msg-abc"
        assert data["model"] == "claude-3-opus"


class TestArbitraryChunkBoundaries:
    @pytest.mark.asyncio
    async def test_arbitrary_chunk_boundaries(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        frame1 = _anthropic_sse("message_start", message_id="msg-1", model="claude-3")
        frame2 = _anthropic_sse("content_block_start", index=0)
        frame3 = _anthropic_sse("content_block_delta", content="Hel", index=0)
        frame4 = _anthropic_sse("content_block_delta", content="lo", index=0)

        all_bytes = frame1 + frame2 + frame3 + frame4

        split_at = 60
        chunk1 = all_bytes[:split_at]
        chunk2 = all_bytes[split_at:]

        raw1 = await transcoder.feed(chunk1)
        raw2 = await transcoder.feed(chunk2)

        all_frames = []
        for raw in [raw1, raw2]:
            combined = b"".join(raw)
            all_frames.extend(_parse_sse_frames(combined))

        content_frames = [
            f
            for f in all_frames
            if f["data"] and f["data"] != "[DONE]" and json.loads(f["data"])["choices"]
        ]
        role_frame = content_frames[0]
        role_data = json.loads(role_frame["data"])
        assert role_data["choices"][0]["delta"]["role"] == "assistant"

        text_frames = [
            f
            for f in content_frames
            if json.loads(f["data"])["choices"][0]["delta"].get("content")
        ]
        all_text = "".join(
            json.loads(f["data"])["choices"][0]["delta"]["content"] for f in text_frames
        )
        assert "Hello" in all_text


class TestToolUseStreaming:
    @pytest.mark.asyncio
    async def test_content_block_start_tool_use_emits_tool_call_delta(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(
            _anthropic_sse(
                "message_start",
                message_id="msg-1",
                model="claude-3",
            )
        )

        raw = await transcoder.feed(
            _anthropic_sse(
                "content_block_start",
                index=0,
                block_type="tool_use",
                tool_id="toolu_xyz",
                tool_name="get_weather",
            )
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        assert len(frames) == 1
        data = json.loads(frames[0]["data"])
        delta = data["choices"][0]["delta"]
        assert delta["role"] == "assistant"
        tool_call = delta["tool_calls"][0]
        assert tool_call["index"] == 0
        assert tool_call["id"].startswith("call_")
        assert tool_call["id"] != "toolu_xyz"
        assert tool_call["type"] == "function"
        assert tool_call["function"]["name"] == "get_weather"
        assert tool_call["function"]["arguments"] == ""

    @pytest.mark.asyncio
    async def test_content_block_delta_input_json_emits_arguments(
        self,
    ) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(
            _anthropic_sse(
                "message_start",
                message_id="msg-1",
                model="claude-3",
            )
        )
        await transcoder.feed(
            _anthropic_sse(
                "content_block_start",
                index=0,
                block_type="tool_use",
                tool_id="toolu_xyz",
                tool_name="get_weather",
            )
        )

        raw = await transcoder.feed(
            _anthropic_sse(
                "content_block_delta",
                index=0,
                partial_json='{"city": "SF"}',
            )
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        assert len(frames) == 1
        data = json.loads(frames[0]["data"])
        delta = data["choices"][0]["delta"]
        assert delta["tool_calls"][0]["index"] == 0
        assert delta["tool_calls"][0]["function"]["arguments"] == '{"city": "SF"}'

    @pytest.mark.asyncio
    async def test_full_tool_use_stream_emits_tool_calls_and_finish(
        self,
    ) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(
            _anthropic_sse(
                "message_start",
                message_id="msg-1",
                model="claude-3",
            )
        )
        tool_start_raw = await transcoder.feed(
            _anthropic_sse(
                "content_block_start",
                index=0,
                block_type="tool_use",
                tool_id="toolu_xyz",
                tool_name="get_weather",
            )
        )
        delta_raw_1 = await transcoder.feed(
            _anthropic_sse(
                "content_block_delta",
                index=0,
                partial_json='{"city":',
            )
        )
        delta_raw_2 = await transcoder.feed(
            _anthropic_sse(
                "content_block_delta",
                index=0,
                partial_json='"SF"}',
            )
        )
        await transcoder.feed(_anthropic_sse("content_block_stop", index=0))

        finish_raw = await transcoder.feed(
            _anthropic_sse(
                "message_delta",
                stop_reason="tool_use",
            )
        )
        flush_raw = await transcoder.flush()

        all_raw = tool_start_raw + delta_raw_1 + delta_raw_2 + finish_raw + flush_raw
        combined = b"".join(all_raw)
        frames = _parse_sse_frames(combined)

        has_done = any(f["data"] == "[DONE]" for f in frames)
        assert has_done

        tool_call_id: str | None = None
        tool_call_name: str | None = None
        tool_args_pieces: list[str] = []
        for f in frames:
            if f["data"] == "[DONE]" or not f["data"]:
                continue
            data = json.loads(f["data"])
            choice = data["choices"][0]
            delta = choice["delta"]
            if "tool_calls" in delta:
                tc = delta["tool_calls"][0]
                if "id" in tc:
                    tool_call_id = tc["id"]
                if tc.get("function", {}).get("name"):
                    tool_call_name = tc["function"]["name"]
                if tc.get("function", {}).get("arguments"):
                    tool_args_pieces.append(tc["function"]["arguments"])
        assert tool_call_id is not None
        assert tool_call_id.startswith("call_")
        assert tool_call_name == "get_weather"
        assert "".join(tool_args_pieces) == '{"city":"SF"}'

    @pytest.mark.asyncio
    async def test_full_stream_with_text_and_tool_use(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(
            _anthropic_sse("message_start", message_id="msg-1", model="claude-3")
        )
        text_start_raw = await transcoder.feed(
            _anthropic_sse("content_block_start", index=0)
        )
        text_delta_raw = await transcoder.feed(
            _anthropic_sse("content_block_delta", content="Looking up...", index=0)
        )
        await transcoder.feed(_anthropic_sse("content_block_stop", index=0))

        tool_start_raw = await transcoder.feed(
            _anthropic_sse(
                "content_block_start",
                index=1,
                block_type="tool_use",
                tool_id="toolu_xyz",
                tool_name="get_weather",
            )
        )
        tool_delta_raw = await transcoder.feed(
            _anthropic_sse(
                "content_block_delta",
                index=1,
                partial_json='{"city": "SF"}',
            )
        )
        await transcoder.feed(_anthropic_sse("content_block_stop", index=1))
        finish_raw = await transcoder.feed(
            _anthropic_sse("message_delta", stop_reason="tool_use")
        )
        flush_raw = await transcoder.flush()

        all_raw = (
            text_start_raw
            + text_delta_raw
            + tool_start_raw
            + tool_delta_raw
            + finish_raw
            + flush_raw
        )
        combined = b"".join(all_raw)
        frames = _parse_sse_frames(combined)

        text_chunks: list[str] = []
        tool_call_id: str | None = None
        tool_call_name: str | None = None
        tool_args: list[str] = []
        finish_reason: str | None = None
        for f in frames:
            if f["data"] == "[DONE]" or not f["data"]:
                continue
            data = json.loads(f["data"])
            choice = data["choices"][0]
            delta = choice["delta"]
            if delta.get("content"):
                text_chunks.append(delta["content"])
            if "tool_calls" in delta:
                tc = delta["tool_calls"][0]
                if "id" in tc:
                    tool_call_id = tc["id"]
                if tc.get("function", {}).get("name"):
                    tool_call_name = tc["function"]["name"]
                if tc.get("function", {}).get("arguments"):
                    tool_args.append(tc["function"]["arguments"])
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

        assert "".join(text_chunks) == "Looking up..."
        assert tool_call_id is not None
        assert tool_call_id.startswith("call_")
        assert tool_call_name == "get_weather"
        assert "".join(tool_args) == '{"city": "SF"}'
        assert finish_reason == "tool_calls"

    @pytest.mark.asyncio
    async def test_multiple_tool_use_blocks_parallel_indices(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(
            _anthropic_sse("message_start", message_id="msg-1", model="claude-3")
        )
        tool_a_raw = await transcoder.feed(
            _anthropic_sse(
                "content_block_start",
                index=0,
                block_type="tool_use",
                tool_id="toolu_a",
                tool_name="get_weather",
            )
        )
        tool_b_raw = await transcoder.feed(
            _anthropic_sse(
                "content_block_start",
                index=1,
                block_type="tool_use",
                tool_id="toolu_b",
                tool_name="get_time",
            )
        )
        await transcoder.feed(_anthropic_sse("content_block_stop", index=0))
        await transcoder.feed(_anthropic_sse("content_block_stop", index=1))
        await transcoder.feed(_anthropic_sse("message_delta", stop_reason="tool_use"))
        await transcoder.flush()

        all_raw = tool_a_raw + tool_b_raw
        ids: list[str] = []
        names: list[str] = []
        for chunk in all_raw:
            for f in _parse_sse_frames(chunk):
                if f["data"] == "[DONE]" or not f["data"]:
                    continue
                data = json.loads(f["data"])
                delta = data["choices"][0]["delta"]
                if "tool_calls" in delta:
                    tc = delta["tool_calls"][0]
                    if "id" in tc:
                        ids.append(tc["id"])
                    if tc.get("function", {}).get("name"):
                        names.append(tc["function"]["name"])
        assert len(ids) == 2
        assert ids[0] != ids[1]
        assert names == ["get_weather", "get_time"]

    @pytest.mark.asyncio
    async def test_tool_arguments_split_across_chunks(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(
            _anthropic_sse("message_start", message_id="msg-1", model="claude-3")
        )
        await transcoder.feed(
            _anthropic_sse(
                "content_block_start",
                index=0,
                block_type="tool_use",
                tool_id="toolu_xyz",
                tool_name="get_weather",
            )
        )

        raw_pieces: list[bytes] = []
        for piece in ['{"city"', ": ", '"SF"}']:
            raw = await transcoder.feed(
                _anthropic_sse(
                    "content_block_delta",
                    index=0,
                    partial_json=piece,
                )
            )
            raw_pieces.extend(raw)

        chunks_args: list[str] = []
        for raw in raw_pieces:
            for f in _parse_sse_frames(raw):
                if f["data"] == "[DONE]" or not f["data"]:
                    continue
                data = json.loads(f["data"])
                args = (
                    data["choices"][0]["delta"]
                    .get("tool_calls", [{}])[0]
                    .get("function", {})
                    .get("arguments")
                )
                if args:
                    chunks_args.append(args)
        assert "".join(chunks_args) == '{"city": "SF"}'

    @pytest.mark.asyncio
    async def test_tool_use_block_id_registered_in_id_map(self) -> None:
        from eggpool.transcoder.context import TranscodeContext

        context = TranscodeContext(
            request_id="req-test",
            client_protocol="openai",
            upstream_protocol="anthropic",
        )
        transcoder = AnthropicToOpenAIStreaming(transcode_context=context)
        await transcoder.feed(
            _anthropic_sse("message_start", message_id="msg-1", model="claude-3")
        )
        await transcoder.feed(
            _anthropic_sse(
                "content_block_start",
                index=0,
                block_type="tool_use",
                tool_id="toolu_input_1",
                tool_name="get_weather",
            )
        )

        assert context.id_map.to_client("toolu_input_1") is not None


class TestPauseTurnSentinel:
    @pytest.mark.asyncio
    async def test_pause_turn_emits_sentinel_tool_call(self) -> None:
        from eggpool.transcoder.context import TranscodeContext

        context = TranscodeContext(
            request_id="req-test",
            client_protocol="openai",
            upstream_protocol="anthropic",
        )
        transcoder = AnthropicToOpenAIStreaming(transcode_context=context)
        await transcoder.feed(
            _anthropic_sse("message_start", message_id="msg-1", model="claude-3")
        )
        raw = await transcoder.feed(
            _anthropic_sse("message_delta", stop_reason="pause_turn")
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        tool_call_frames = []
        finish_frames = []
        for f in frames:
            if f.get("done"):
                continue
            data = json.loads(f["data"]) if f["data"] else {}
            choices = data.get("choices", [])
            if choices:
                delta = choices[0].get("delta", {})
                if "tool_calls" in delta:
                    tool_call_frames.append(data)
                if choices[0].get("finish_reason") is not None:
                    finish_frames.append(data)

        assert len(tool_call_frames) >= 1, (
            "Expected at least one tool_call delta for pause_turn sentinel"
        )
        first_tc = tool_call_frames[0]["choices"][0]["delta"]["tool_calls"][0]
        assert first_tc["type"] == "function"
        assert first_tc["function"]["name"] == "__eggpool_pause_turn__"
        assert first_tc["id"].startswith("call_")

        assert len(finish_frames) == 1
        assert finish_frames[0]["choices"][0]["finish_reason"] == "tool_calls"

    @pytest.mark.asyncio
    async def test_pause_turn_appends_loss_warning(self) -> None:
        from eggpool.transcoder.context import TranscodeContext

        context = TranscodeContext(
            request_id="req-test",
            client_protocol="openai",
            upstream_protocol="anthropic",
        )
        transcoder = AnthropicToOpenAIStreaming(transcode_context=context)
        await transcoder.feed(
            _anthropic_sse("message_start", message_id="msg-1", model="claude-3")
        )
        await transcoder.feed(_anthropic_sse("message_delta", stop_reason="pause_turn"))

        pause_warnings = [
            w for w in context.loss_warnings if w.get("kind") == "pause_turn"
        ]
        assert len(pause_warnings) == 1
        assert pause_warnings[0]["to"] == "tool_calls"

    @pytest.mark.asyncio
    async def test_pause_turn_arguments_are_empty_json(self) -> None:
        from eggpool.transcoder.context import TranscodeContext

        context = TranscodeContext(
            request_id="req-test",
            client_protocol="openai",
            upstream_protocol="anthropic",
        )
        transcoder = AnthropicToOpenAIStreaming(transcode_context=context)
        await transcoder.feed(
            _anthropic_sse("message_start", message_id="msg-1", model="claude-3")
        )
        raw = await transcoder.feed(
            _anthropic_sse("message_delta", stop_reason="pause_turn")
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        for f in frames:
            if f.get("done"):
                continue
            data = json.loads(f["data"]) if f["data"] else {}
            choices = data.get("choices", [])
            if choices:
                delta = choices[0].get("delta", {})
                tc_list = delta.get("tool_calls", [])
                for tc in tc_list:
                    func = tc.get("function", {})
                    if func.get("arguments"):
                        assert json.loads(func["arguments"]) == {}
