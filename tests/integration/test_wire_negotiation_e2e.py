"""Deterministic end-to-end wire migration and observability acceptance."""

from __future__ import annotations

import asyncio
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


@respx.mock
@pytest.mark.asyncio()
async def test_concurrent_stale_requests_share_wire_discovery(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Followers share only the leader's accepted wire decision."""
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
    response_count = 0
    response_barrier = asyncio.Event()

    async def stale_responses(_request: httpx.Request) -> httpx.Response:
        nonlocal response_count
        response_count += 1
        if response_count == 3:
            response_barrier.set()
        await response_barrier.wait()
        return httpx.Response(
            404,
            json={
                "error": {
                    "type": "invalid_request_error",
                    "message": "unsupported endpoint for this surface",
                }
            },
        )

    respx.post(f"{UPSTREAM_BASE}/responses").mock(side_effect=stale_responses)
    respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
        side_effect=lambda _request: _chat_success_response()
    )

    from httpx import ASGITransport

    try:
        transport = ASGITransport(app=result.application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            payload = {
                "model": "migrating-model",
                "messages": [{"role": "user", "content": "hello"}],
            }
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/v1/chat/completions",
                        headers={"Authorization": "Bearer rt-test-key"},
                        json=payload,
                    )
                    for _ in range(3)
                )
            )
            assert all(response.status_code == 200 for response in responses), [
                response.text for response in responses
            ]
            steady = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer rt-test-key"},
                json=payload,
            )
            assert steady.status_code == 200, steady.text
    finally:
        await result.runtime_manager.shutdown()
        await result.db.disconnect()
        await result.httpx_client.aclose()

    paths = [item.path for item in observations]
    assert paths.count("/responses") == 3
    assert paths.count("/chat/completions") == 4
    assert all(item.account_id == "rt-acct-1" for item in observations)
    assert all(item.status_code in {200, 404} for item in observations)
    assert all(item.attempt_ordinal in {1, 2} for item in observations[:6])
    assert observations[-1].attempt_ordinal == 1
    assert all(item.wire_surface == "openai_responses" for item in observations[:3])
    assert all(
        item.wire_surface == "openai_chat_completions" for item in observations[3:]
    )
    health = result.health_manager.get_health_stats("rt-acct-1")
    assert health["is_healthy"] is True

    counters = result.coordinator._wire_profile_resolver.snapshot()["counters"]
    assert counters["wire_negotiation_attempted:leader"] == 1
    assert counters["wire_singleflight_follower:follower"] >= 2


def _chat_success_response() -> httpx.Response:
    return httpx.Response(200, json=_chat_success())


@respx.mock
@pytest.mark.asyncio()
async def test_rate_limited_leader_does_not_probe_another_surface(
    migration_app: tuple[FastAPI, list[Any]],
) -> None:
    app, observations = migration_app
    response_count = 0
    response_barrier = asyncio.Event()

    async def stale_responses(_request: httpx.Request) -> httpx.Response:
        nonlocal response_count
        response_count += 1
        if response_count == 2:
            response_barrier.set()
        await response_barrier.wait()
        return httpx.Response(
            404,
            json={
                "error": {
                    "type": "invalid_request_error",
                    "message": "unsupported endpoint for this surface",
                }
            },
        )

    respx.post(f"{UPSTREAM_BASE}/responses").mock(side_effect=stale_responses)
    respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "30"},
            json={"error": {"message": "slow down"}},
        )
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
        responses = await asyncio.gather(
            *(
                client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer rt-test-key"},
                    json=payload,
                )
                for _ in range(2)
            )
        )

    assert sorted(response.status_code for response in responses) == [404, 429]
    paths = [item.path for item in observations]
    assert paths.count("/responses") == 2
    assert paths.count("/chat/completions") == 1
    assert len(paths) == 3


@respx.mock
@pytest.mark.asyncio()
async def test_provider_gate_serializes_discovery_but_not_known_good_inference(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REAL_RUNTIME_KEY", "rt-test-key")
    observations: list[Any] = []
    model_ids = ("migrating-model-a", "migrating-model-b")
    wire_surfaces = {key: dict(value) for key, value in _WIRE_SURFACES.items()}
    spec = RuntimeAppSpec(
        account_names=("rt-acct-1",),
        models=tuple(ModelSpec(model_id=model_id) for model_id in model_ids),
        providers=(
            ProviderSpec(
                provider_id="synthetic-wire-provider",
                protocols=("openai",),
                static_models=tuple(
                    ModelSpec(model_id=model_id) for model_id in model_ids
                ),
                account_names=("rt-acct-1",),
                wire_surfaces=wire_surfaces,
                model_wire={
                    model_id: {"preferred_surface": "openai_responses"}
                    for model_id in model_ids
                },
            ),
        ),
        wire_negotiation_overrides={
            "max_concurrent_per_provider": 1,
            "min_negotiation_interval_s": 0,
        },
        outbound_observer=observations.append,
        wire_runtime_enabled=True,
    )
    result = await build_runtime_app(spec, tmp_path=tmp_path)
    response_count = 0
    response_barrier = asyncio.Event()
    discovery_active = 0
    discovery_maximum = 0

    async def stale_responses(_request: httpx.Request) -> httpx.Response:
        nonlocal response_count
        response_count += 1
        if response_count == 2:
            response_barrier.set()
        await response_barrier.wait()
        return httpx.Response(404, json={"error": {"message": "stale surface"}})

    async def delayed_chat(_request: httpx.Request) -> httpx.Response:
        nonlocal discovery_active, discovery_maximum
        discovery_active += 1
        discovery_maximum = max(discovery_maximum, discovery_active)
        await asyncio.sleep(0.05)
        discovery_active -= 1
        return _chat_success_response()

    respx.post(f"{UPSTREAM_BASE}/responses").mock(side_effect=stale_responses)
    respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=delayed_chat)

    from httpx import ASGITransport

    try:
        transport = ASGITransport(app=result.application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:

            def request(model_id: str) -> Any:
                return client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer rt-test-key"},
                    json={
                        "model": model_id,
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )

            first_responses = await asyncio.gather(
                *(request(model_id) for model_id in model_ids)
            )
            assert all(response.status_code == 200 for response in first_responses)
            assert discovery_maximum == 1

            discovery_maximum = 0
            steady_responses = await asyncio.gather(
                *(request(model_id) for model_id in model_ids)
            )
            assert all(response.status_code == 200 for response in steady_responses)
            assert discovery_maximum == 2
    finally:
        await result.runtime_manager.shutdown()
        await result.db.disconnect()
        await result.httpx_client.aclose()

    paths = [item.path for item in observations]
    assert paths.count("/responses") == 2
    assert paths.count("/chat/completions") == 4
    assert all(item.account_id == "rt-acct-1" for item in observations)
