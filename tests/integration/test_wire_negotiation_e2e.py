"""Deterministic end-to-end wire migration and observability acceptance."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio
import respx

from tests.helpers.real_runtime import (
    UPSTREAM_BASE,
    ModelSpec,
    ProviderSpec,
    RuntimeAppSpec,
    build_runtime_app,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI

pytestmark = [pytest.mark.integration]

_WIRE_SURFACES = {
    "openai_chat_completions": {
        "path_template": "/chat/completions",
        "priority": 100,
        "auth": {"mode": "bearer"},
    },
    "openai_responses": {
        "path_template": "/responses",
        "priority": 90,
        "auth": {"mode": "bearer"},
    },
}


def _responses_success() -> dict[str, Any]:
    return {
        "id": "resp-migration-1",
        "object": "response",
        "status": "completed",
        "model": "migrating-model",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Responses OK"}],
            }
        ],
        "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
    }


def _chat_success() -> dict[str, Any]:
    return {
        "id": "chat-migration-1",
        "object": "chat.completion",
        "model": "migrating-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Chat OK"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


@pytest_asyncio.fixture()
async def migration_app(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[tuple[FastAPI, list[Any]], None]:
    monkeypatch.setenv("REAL_RUNTIME_KEY", "rt-test-key")
    observations: list[Any] = []
    spec = RuntimeAppSpec(
        account_names=("rt-acct-1",),
        models=(ModelSpec(model_id="migrating-model", protocol="openai"),),
        providers=(
            ProviderSpec(
                provider_id="synthetic-wire-provider",
                protocols=("openai",),
                static_models=(
                    ModelSpec(model_id="migrating-model", protocol="openai"),
                ),
                account_names=("rt-acct-1",),
                wire_surfaces=_WIRE_SURFACES,
                model_wire={
                    "migrating-model": {
                        "preferred_surface": "openai_responses",
                    }
                },
            ),
        ),
        outbound_observer=observations.append,
        wire_runtime_enabled=True,
    )
    result = await build_runtime_app(spec, tmp_path=tmp_path)
    try:
        yield result.application, observations
    finally:
        await result.runtime_manager.shutdown()
        await result.db.disconnect()
        await result.httpx_client.aclose()


@respx.mock
@pytest.mark.asyncio()
async def test_stale_wire_profile_relearns_without_restart(
    migration_app: tuple[FastAPI, list[Any]],
) -> None:
    """A safe surface rejection moves one account to its new profile."""
    app, observations = migration_app
    phase = "responses"

    def responses_handler(_request: httpx.Request) -> httpx.Response:
        if phase == "responses":
            return httpx.Response(200, json=_responses_success())
        return httpx.Response(
            404,
            json={
                "error": {
                    "type": "invalid_request_error",
                    "message": "unsupported endpoint for this surface",
                }
            },
        )

    respx.post(f"{UPSTREAM_BASE}/responses").mock(side_effect=responses_handler)
    respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_success())
    )

    from httpx import ASGITransport

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        payload = {
            "model": "migrating-model",
            "messages": [{"role": "user", "content": "hello"}],
        }
        first = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer rt-test-key"},
            json=payload,
        )
        assert first.status_code == 200, (
            first.text,
            [(item.path, item.status_code, item.wire_surface) for item in observations],
        )
        assert first.json()["choices"][0]["message"]["content"] == "Responses OK"

        phase = "chat"
        second = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer rt-test-key"},
            json=payload,
        )
        assert second.status_code == 200, (
            second.text,
            [
                (
                    item.path,
                    item.status_code,
                    item.wire_surface,
                    item.candidate_surfaces,
                )
                for item in observations
            ],
        )
        assert second.json()["choices"][0]["message"]["content"] == "Chat OK"

        third = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer rt-test-key"},
            json=payload,
        )
        assert third.status_code == 200, third.text

    assert [item.path for item in observations] == [
        "/responses",
        "/responses",
        "/chat/completions",
        "/chat/completions",
    ]
    assert [item.wire_surface for item in observations] == [
        "openai_responses",
        "openai_responses",
        "openai_chat_completions",
        "openai_chat_completions",
    ]
    assert observations[1].status_code == 404
    assert observations[2].attempt_ordinal == 2
    assert observations[3].attempt_ordinal == 1
    assert all(item.account_id == "rt-acct-1" for item in observations)
    assert all(item.auth_scheme == "bearer" for item in observations)
    assert all("rt-test-key" not in repr(item) for item in observations)

    health = app.state.health_manager.get_health_stats("rt-acct-1")
    assert health["is_healthy"] is True
    assert health["disabled_reason"] == ""
