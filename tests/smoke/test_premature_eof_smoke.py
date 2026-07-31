"""Smoke: premature EOF on a streaming response is not marked completed."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from tests.helpers.real_runtime import UPSTREAM_BASE

if TYPE_CHECKING:
    from fastapi import FastAPI


def _premature_eof_body() -> str:
    """SSE content followed by clean EOF with no [DONE] terminal marker."""
    return (
        'data: {"id":"smoke-eof","object":"chat.completion.chunk",'
        '"model":"gpt-4",'
        '"choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}\n\n'
    )


@respx.mock
@pytest.mark.asyncio()
async def test_premature_eof_not_completed(real_runtime_app: FastAPI) -> None:
    """A clean upstream EOF without [DONE] must not be reported as completed."""
    respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            text=_premature_eof_body(),
            headers={"content-type": "text/event-stream"},
        )
    )

    from httpx import ASGITransport

    transport = ASGITransport(app=real_runtime_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        try:
            resp = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer rt-test-key"},
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "stream": True,
                },
            )
        except RuntimeError as exc:
            # ASGITransport reports an exception after response headers have
            # started; production servers likewise terminate the iterator.
            assert "PrematureStreamEOFError" in repr(exc.__cause__)
            body = ""
        else:
            body = resp.text

    # The response may be 200 (headers already sent) or an error depending
    # on how the coordinator handles midstream failure.  Either way, the
    # downstream body must NOT contain a successful terminal marker.
    # A normal successful OpenAI stream ends with "data: [DONE]"
    # After premature EOF the body should not have that marker.
    # The body may contain an error JSON or be empty, but [DONE] is forbidden.
    assert "[DONE]" not in body, (
        "Premature EOF stream must not contain [DONE] terminal marker"
    )
