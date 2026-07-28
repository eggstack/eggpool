"""Smoke: one OpenAI non-stream request through the real Eggpool harness."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from tests.helpers.real_runtime import UPSTREAM_BASE

if TYPE_CHECKING:
    from fastapi import FastAPI


@respx.mock
@pytest.mark.asyncio()
async def test_openai_nonstream_smoke(real_runtime_app: FastAPI) -> None:
    """Send one non-streaming OpenAI request through the full Eggpool stack."""
    upstream_response = {
        "id": "smoke-chatcmpl-1",
        "object": "chat.completion",
        "model": "gpt-4",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello from smoke"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }
    respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=upstream_response)
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
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == "Hello from smoke"
    assert body["usage"]["total_tokens"] == 8
