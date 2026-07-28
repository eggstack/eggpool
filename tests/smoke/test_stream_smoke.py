"""Smoke: one streaming OpenAI SSE request through the real Eggpool stack."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from tests.helpers.real_runtime import UPSTREAM_BASE

if TYPE_CHECKING:
    from fastapi import FastAPI


def _sse_response_body() -> str:
    return (
        'data: {"id":"smoke-stream","object":"chat.completion.chunk",'
        '"model":"gpt-4",'
        '"choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":null}]}\n\n'
        'data: {"id":"smoke-stream","object":"chat.completion.chunk",'
        '"model":"gpt-4",'
        '"choices":[{"index":0,"delta":{"content":" there"},"finish_reason":null}]}\n\n'
        'data: {"id":"smoke-stream","object":"chat.completion.chunk",'
        '"model":"gpt-4",'
        '"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2,'
        '"total_tokens":7}}\n\n'
        "data: [DONE]\n\n"
    )


@respx.mock
@pytest.mark.asyncio()
async def test_openai_stream_smoke(real_runtime_app: FastAPI) -> None:
    """Send one streaming OpenAI request through the full Eggpool stack."""
    respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            text=_sse_response_body(),
            headers={"content-type": "text/event-stream"},
        )
    )

    from httpx import ASGITransport

    transport = ASGITransport(app=real_runtime_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer rt-test-key"},
            json={
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        )

    assert resp.status_code == 200
    body = resp.text
    assert "Hi" in body
    assert "[DONE]" in body or "data:" in body
