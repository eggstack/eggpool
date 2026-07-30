"""Smoke: one Anthropic non-stream request through the real Eggpool harness."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio
import respx

from tests.helpers.real_runtime import (
    UPSTREAM_BASE,
    ModelSpec,
    RuntimeAppSpec,
    build_runtime_app,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI


@pytest_asyncio.fixture()
async def anthropic_runtime_app(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[FastAPI, None]:
    """Provide an Eggpool runtime with an Anthropic model seeded."""
    monkeypatch.setenv("REAL_RUNTIME_KEY", "rt-test-key")
    spec = RuntimeAppSpec(
        models=(
            ModelSpec(
                model_id="claude-sonnet-4-20250514",
                protocol="anthropic",
            ),
        ),
    )
    result = await build_runtime_app(spec=spec, tmp_path=tmp_path)
    yield result.application
    await result.db.disconnect()
    await result.httpx_client.aclose()


@respx.mock
@pytest.mark.asyncio()
async def test_anthropic_nonstream_smoke(
    anthropic_runtime_app: FastAPI,
) -> None:
    """Send one non-streaming Anthropic request through the full Eggpool stack."""
    upstream_response = {
        "id": "smoke-msg-1",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "Hello from Anthropic smoke"}],
        "model": "claude-sonnet-4-20250514",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }
    respx.post(f"{UPSTREAM_BASE}/messages").mock(
        return_value=httpx.Response(200, json=upstream_response)
    )

    from httpx import ASGITransport

    transport = ASGITransport(app=anthropic_runtime_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/v1/messages",
            headers={
                "Authorization": "Bearer rt-test-key",
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    # Anthropic response format: content is a list of blocks
    assert body["content"][0]["text"] == "Hello from Anthropic smoke"
    assert body["usage"]["output_tokens"] == 3
