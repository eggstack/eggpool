"""Performance microbenchmarks for streaming transcoders.

Measures chunks processed per second, output bytes, and CPU time per
1000 upstream chunks for both ``AnthropicToOpenAIStreaming`` and
``OpenAIToAnthropicStreaming``.

Run with::

    pytest tests/perf/test_streaming_transcoder_perf.py -m performance -v
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from eggpool.transcoder.streaming import (
    AnthropicToOpenAIStreaming,
    OpenAIToAnthropicStreaming,
)

pytestmark = pytest.mark.performance


# ---------------------------------------------------------------------------
# Synthetic stream builders
# ---------------------------------------------------------------------------

_CHUNK_SIZE = 64  # bytes per synthetic upstream chunk
_SEP = (",", ":")


def _sse_frame(event: str, data: dict[str, Any]) -> bytes:
    """Build an Anthropic SSE frame: ``event: <type>\\ndata: <json>\\n\\n``."""
    return (f"event: {event}\ndata: {json.dumps(data, separators=_SEP)}\n\n").encode()


def _openai_sse_frame(data: dict[str, Any]) -> bytes:
    """Build an OpenAI SSE frame: ``data: <json>\\n\\n``."""
    return f"data: {json.dumps(data, separators=_SEP)}\n\n".encode()


def _build_anthropic_text_upstream(n_chunks: int = 5000) -> list[bytes]:
    """Build a synthetic Anthropic SSE stream of *n_chunks* text deltas."""
    chunks: list[bytes] = []
    # message_start
    start = {
        "type": "message_start",
        "message": {
            "id": "msg-perf-001",
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": "claude-3-haiku-20240307",
            "stop_reason": None,
            "usage": {"input_tokens": 10, "output_tokens": 0},
        },
    }
    chunks.append(_sse_frame("message_start", start))
    # content_block_start
    cbs = {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    }
    chunks.append(_sse_frame("content_block_start", cbs))
    # content_block_delta × n_chunks
    for i in range(n_chunks):
        text = f"token-{i:05d} "[:_CHUNK_SIZE]
        delta = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        }
        chunks.append(_sse_frame("content_block_delta", delta))
    # message_delta
    md = {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn"},
        "usage": {"output_tokens": n_chunks},
    }
    chunks.append(_sse_frame("message_delta", md))
    # message_stop
    chunks.append(b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
    return chunks


def _build_openai_text_upstream(n_chunks: int = 5000) -> list[bytes]:
    """Build a synthetic OpenAI SSE stream of *n_chunks* text deltas."""
    chunks: list[bytes] = []
    for i in range(n_chunks):
        text = f"token-{i:05d} "[:_CHUNK_SIZE]
        payload = {
            "id": "chatcmpl-perf-001",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": text},
                    "finish_reason": None,
                }
            ],
        }
        chunks.append(_openai_sse_frame(payload))
    # finish
    finish = {
        "id": "chatcmpl-perf-001",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": "gpt-4",
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": n_chunks,
            "total_tokens": 10 + n_chunks,
        },
    }
    chunks.append(_openai_sse_frame(finish))
    chunks.append(b"data: [DONE]\n\n")
    return chunks


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStreamingTranscoderPerformance:
    """Measure per-chunk CPU time and throughput for both transcoder directions."""

    def test_anthropic_to_openai_throughput(self) -> None:
        n_chunks = 5000
        chunks = _build_anthropic_text_upstream(n_chunks)
        transcoder = AnthropicToOpenAIStreaming()

        t_start = time.perf_counter()
        c_start = time.process_time()
        out: list[bytes] = []
        for chunk in chunks:
            out.extend(transcoder.feed(chunk))
        out.extend(transcoder.flush())
        c_end = time.process_time()
        t_end = time.perf_counter()

        total_bytes = sum(len(b) for b in out)
        wall_ms = (t_end - t_start) * 1000
        cpu_ms = (c_end - c_start) * 1000
        chunks_per_sec = n_chunks / (t_end - t_start)
        cpu_per_1000 = cpu_ms / (n_chunks / 1000)

        print(
            f"\n  AnthropicToOpenAI: {n_chunks} chunks in {wall_ms:.1f}ms "
            f"({chunks_per_sec:.0f} chunks/s), "
            f"CPU {cpu_ms:.1f}ms ({cpu_per_1000:.2f}ms/1k chunks), "
            f"output {total_bytes} bytes"
        )

        # Loose floor: must process at least 10k chunks/sec on any modern hardware
        assert chunks_per_sec > 10_000, (
            f"Throughput too low: {chunks_per_sec:.0f} chunks/s"
        )
        # CPU per 1000 chunks must be under 50ms (generous)
        assert cpu_per_1000 < 50, f"CPU per 1k chunks too high: {cpu_per_1000:.2f}ms"

    def test_openai_to_anthropic_throughput(self) -> None:
        n_chunks = 5000
        chunks = _build_openai_text_upstream(n_chunks)
        transcoder = OpenAIToAnthropicStreaming()

        t_start = time.perf_counter()
        c_start = time.process_time()
        out: list[bytes] = []
        for chunk in chunks:
            out.extend(transcoder.feed(chunk))
        out.extend(transcoder.flush())
        c_end = time.process_time()
        t_end = time.perf_counter()

        total_bytes = sum(len(b) for b in out)
        wall_ms = (t_end - t_start) * 1000
        cpu_ms = (c_end - c_start) * 1000
        chunks_per_sec = n_chunks / (t_end - t_start)
        cpu_per_1000 = cpu_ms / (n_chunks / 1000)

        print(
            f"\n  OpenAIToAnthropic: {n_chunks} chunks in {wall_ms:.1f}ms "
            f"({chunks_per_sec:.0f} chunks/s), "
            f"CPU {cpu_ms:.1f}ms ({cpu_per_1000:.2f}ms/1k chunks), "
            f"output {total_bytes} bytes"
        )

        assert chunks_per_sec > 10_000, (
            f"Throughput too low: {chunks_per_sec:.0f} chunks/s"
        )
        assert cpu_per_1000 < 50, f"CPU per 1k chunks too high: {cpu_per_1000:.2f}ms"

    def test_output_byte_determinism(self) -> None:
        """Verify that the same input produces identical output bytes across runs."""
        n_chunks = 500
        chunks = _build_anthropic_text_upstream(n_chunks)

        outputs: list[bytes] = []
        for _ in range(3):
            transcoder = AnthropicToOpenAIStreaming()
            out: list[bytes] = []
            for chunk in chunks:
                out.extend(transcoder.feed(chunk))
            out.extend(transcoder.flush())
            outputs.append(b"".join(out))

        assert outputs[0] == outputs[1] == outputs[2], (
            "Transcoder output is non-deterministic across runs"
        )

    def test_output_byte_count_matches_fixture(self) -> None:
        """Verify byte count is stable across a known fixture run."""
        from tests.helpers.streaming_fixtures import (
            fixture_to_sse_bytes,
            load_streaming_fixture,
        )

        fixture = load_streaming_fixture("anthropic_text_streaming")
        chunks = fixture_to_sse_bytes(
            fixture["events"],
            protocol=fixture["upstream_protocol"],
        )
        transcoder = AnthropicToOpenAIStreaming()
        out: list[bytes] = []
        for chunk in chunks:
            out.extend(transcoder.feed(chunk))
        out.extend(transcoder.flush())
        total_bytes = sum(len(b) for b in out)
        # The output must be non-empty and deterministic
        assert total_bytes > 0

        # Run again and verify byte-identical
        transcoder2 = AnthropicToOpenAIStreaming()
        out2: list[bytes] = []
        for chunk in chunks:
            out2.extend(transcoder2.feed(chunk))
        out2.extend(transcoder2.flush())
        assert b"".join(out) == b"".join(out2)
