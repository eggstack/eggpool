"""Section 10: Preserve raw non-streaming responses."""

from __future__ import annotations

import asyncio

import pytest

from eggpool.api.proxy_request import ProxyStreamingResponse
from eggpool.request.coordinator import PreparedProxyResponse
from eggpool.request.response_handoff import ResponseHandoffState


class TestRawResponseRendering:
    """Tests that non-streaming responses preserve upstream bytes."""

    def test_body_is_raw_bytes(self) -> None:
        """PreparedProxyResponse body should be raw bytes."""
        body = b'{"id":"chatcmpl-123","object":"chat.completion"}'
        result = PreparedProxyResponse(
            status_code=200,
            headers=[("content-type", "application/json")],
            body=body,
        )
        assert result.body == body
        assert isinstance(result.body, bytes)

    def test_non_json_body_preserved(self) -> None:
        """Non-JSON upstream error bodies should be preserved as bytes."""
        body = b"This is not JSON, just plain text error"
        result = PreparedProxyResponse(
            status_code=500,
            headers=[("content-type", "text/plain")],
            body=body,
        )
        assert result.body == body

    def test_binary_body_preserved(self) -> None:
        """Binary bodies should be preserved."""
        body = bytes(range(256))
        result = PreparedProxyResponse(
            status_code=200,
            headers=[("content-type", "application/octet-stream")],
            body=body,
        )
        assert result.body == body

    def test_json_whitespace_preserved(self) -> None:
        """JSON whitespace should be byte-identical."""
        body = b'{  "key":  "value"  }'
        result = PreparedProxyResponse(
            status_code=200,
            headers=[("content-type", "application/json")],
            body=body,
        )
        assert result.body == body

    def test_content_type_passthrough(self) -> None:
        """Upstream content-type header should be preserved."""
        headers = [
            ("content-type", "application/json; charset=utf-8"),
            ("x-request-id", "req-123"),
        ]
        result = PreparedProxyResponse(
            status_code=200,
            headers=headers,
            body=b"{}",
        )
        assert any(
            k == "content-type" and v == "application/json; charset=utf-8"
            for k, v in result.headers
        )


def _scope() -> dict[str, object]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 80),
    }


async def _never_receive() -> dict[str, object]:
    await asyncio.Event().wait()
    return {"type": "http.disconnect"}


@pytest.mark.asyncio
async def test_stream_response_start_marks_handoff_before_first_body() -> None:
    state = ResponseHandoffState()
    observed: list[bool] = []

    async def body():
        observed.append(state.started)
        yield b"chunk"

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    response = ProxyStreamingResponse(
        body(),
        status_code=201,
        headers={"x-test": "preserved"},
        response_handoff=state,
    )
    await response(_scope(), _never_receive, send)  # type: ignore[arg-type]

    assert state.started
    assert observed == [True]
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 201
    assert (b"x-test", b"preserved") in sent[0]["headers"]  # type: ignore[operator]


@pytest.mark.asyncio
async def test_empty_started_stream_has_handoff_without_body_bytes() -> None:
    state = ResponseHandoffState()

    async def empty_body():
        if False:
            yield b"unreachable"

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    response = ProxyStreamingResponse(
        empty_body(),
        response_handoff=state,
    )
    await response(_scope(), _never_receive, send)  # type: ignore[arg-type]

    assert state.started
    assert [
        message["body"] for message in sent if message["type"] == "http.response.body"
    ] == [b""]


@pytest.mark.asyncio
async def test_stream_failure_after_response_start_is_post_handoff() -> None:
    state = ResponseHandoffState()

    async def broken_body():
        raise RuntimeError("local stream translation failure")
        yield b"unreachable"

    async def send(message: dict[str, object]) -> None:
        del message

    response = ProxyStreamingResponse(
        broken_body(),
        response_handoff=state,
    )
    with pytest.raises(RuntimeError, match="translation failure"):
        await response(_scope(), _never_receive, send)  # type: ignore[arg-type]
    assert state.started
