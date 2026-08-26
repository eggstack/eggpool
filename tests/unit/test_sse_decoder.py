"""Contract tests for the shared, protocol-neutral SSE decoder."""

from __future__ import annotations

from eggpool.proxy.sse import SSEDecoder


def _frames(payload: bytes, cuts: list[int]) -> list[tuple[str | None, str]]:
    decoder = SSEDecoder()
    start = 0
    result = []
    for end in cuts:
        result.extend(decoder.feed(payload[start:end]))
        start = end
    result.extend(decoder.feed(payload[start:]))
    result.extend(decoder.finish().frames)
    return [(item.frame.event, item.frame.data) for item in result]


def test_arbitrary_chunk_boundaries_and_multiline_data_are_stable() -> None:
    payload = b'event: message\rdata: {"a":\rdata: 1}\r\rdata: [DONE]\n\n'
    expected = [("message", '{"a":\n1}'), (None, "[DONE]")]
    assert _frames(payload, list(range(1, len(payload)))) == expected


def test_comments_and_unknown_fields_are_retained_without_json_parsing() -> None:
    result = SSEDecoder().feed(b": heartbeat\nretry: 1000\ndata: {}\n\n")
    assert len(result) == 1
    assert result[0].frame.fields == (
        ("", " heartbeat"),
        ("retry", "1000"),
        ("data", "{}"),
    )


def test_oversized_input_is_discarded_and_eof_is_bounded() -> None:
    decoder = SSEDecoder(max_frame_bytes=8)
    decoder.feed(b"data: 123456789")
    result = decoder.finish()
    assert result.discarded_frame_count == 1
    assert result.incomplete_frame


def test_json_object_is_cached_on_the_shared_frame() -> None:
    decoded = SSEDecoder().feed(b'data: {"value": 1}\n\n')[0]
    first = decoded.json_object()
    second = decoded.json_object()
    assert first is second


def test_final_line_without_newline_is_emitted_at_eof() -> None:
    decoder = SSEDecoder()
    decoder.feed(b"data: [DONE]")
    eof = decoder.finish()

    assert len(eof.frames) == 1
    assert eof.frames[0].frame.data == "[DONE]"
    assert not eof.incomplete_frame


def test_final_line_after_newline_is_emitted_without_blank_line() -> None:
    decoder = SSEDecoder()
    decoder.feed(b"data: [DONE]\n")
    eof = decoder.finish()

    assert len(eof.frames) == 1
    assert eof.frames[0].frame.data == "[DONE]"
    assert not eof.incomplete_frame
