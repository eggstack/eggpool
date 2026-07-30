"""Smoke: upstream validation error followed by a healthy unrelated request."""

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
async def test_upstream_validation_error_then_healthy_request(
    real_runtime_app: FastAPI,
) -> None:
    """An invalid-model upstream error must not poison a subsequent healthy request."""
    from httpx import ASGITransport

    # First request: invalid model triggers upstream 400
    respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {"message": "invalid model", "type": "invalid_request_error"}
            },
        )
    )

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

    # The upstream error should be relayed to the client
    assert resp.status_code == 400

    # Now clear the mock and set up a healthy response for the second request
    respx.clear()

    healthy_response = {
        "id": "smoke-recovery-1",
        "object": "chat.completion",
        "model": "gpt-4",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Recovered"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }
    respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=healthy_response)
    )

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        resp2 = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer rt-test-key"},
            json={
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    # The second request must succeed — the first error must not poison routing
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["choices"][0]["message"]["content"] == "Recovered"
