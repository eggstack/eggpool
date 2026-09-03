"""Deterministic end-to-end wire migration and observability acceptance."""

from __future__ import annotations

import asyncio
import json
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

_UNHINTED_WIRE_SURFACES = {
    "openai_responses": {
        "path_template": "/responses",
        "priority": 10,
        "auth": {"mode": "bearer"},
    },
    "openai_chat_completions": {
        "path_template": "/chat/completions",
        "priority": 20,
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


def _responses_cross_surface_success() -> dict[str, Any]:
    return {
        "id": "resp-cross-surface-1",
        "object": "response",
        "status": "completed",
        "model": "responses-target",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Responses bridge"}],
            }
        ],
        "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
    }


def _anthropic_cross_surface_success() -> dict[str, Any]:
    return {
        "id": "msg-cross-surface-1",
        "type": "message",
        "role": "assistant",
        "model": "messages-target",
        "content": [{"type": "text", "text": "Messages bridge"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 4, "output_tokens": 2},
    }


def _responses_stream() -> bytes:
    return b"".join(
        (
            b'event: response.created\ndata: {"type":"response.created",'
            b'"response":{"id":"resp-stream-1","model":"responses-target"}}\n\n',
            b"event: response.output_text.delta\ndata: "
            b'{"type":"response.output_text.delta",'
            b'"delta":"streamed"}\n\n',
            b'event: response.completed\ndata: {"type":"response.completed",'
            b'"response":{"id":"resp-stream-1","status":"completed",'
            b'"usage":{"input_tokens":4,"output_tokens":2,"total_tokens":6}}}\n\n',
        )
    )


def _anthropic_stream() -> bytes:
    return b"".join(
        (
            b'event: message_start\ndata: {"type":"message_start","message":'
            b'{"id":"msg-stream-1","model":"messages-target"}}\n\n',
            b'event: content_block_start\ndata: {"type":"content_block_start",'
            b'"index":0,"content_block":{"type":"text","text":""}}\n\n',
            b'event: content_block_delta\ndata: {"type":"content_block_delta",'
            b'"index":0,"delta":{"type":"text_delta","text":"streamed"}}\n\n',
            b'event: content_block_stop\ndata: {"type":"content_block_stop",'
            b'"index":0}\n\n',
            b'event: message_delta\ndata: {"type":"message_delta",'
            b'"delta":{"stop_reason":"end_turn"},"usage":{"input_tokens":4,'
            b'"output_tokens":2}}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        )
    )


@respx.mock
@pytest.mark.asyncio()
async def test_anthropic_messages_client_bridges_to_responses_upstream(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The coordinator adapts Messages requests and streams to Responses."""
    monkeypatch.setenv("REAL_RUNTIME_KEY", "rt-test-key")
    observations: list[Any] = []
    model = ModelSpec(model_id="responses-target", protocol="openai")
    spec = RuntimeAppSpec(
        account_names=("rt-acct-1",),
        models=(model,),
        providers=(
            ProviderSpec(
                provider_id="responses-provider",
                protocols=("openai",),
                static_models=(model,),
                account_names=("rt-acct-1",),
                wire_surfaces={
                    "openai_responses": {
                        "path_template": "/responses",
                        "priority": 10,
                        "auth": {"mode": "bearer"},
                    }
                },
            ),
        ),
        outbound_observer=observations.append,
        wire_runtime_enabled=True,
    )
    result = await build_runtime_app(spec, tmp_path=tmp_path)
    try:
        seen_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_requests.append(request)
            if request.content and b'"stream":true' in request.content:
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=_responses_stream(),
                )
            return httpx.Response(200, json=_responses_cross_surface_success())

        respx.post(f"{UPSTREAM_BASE}/responses").mock(side_effect=handler)
        from httpx import ASGITransport

        transport = ASGITransport(app=result.application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            payload = {
                "model": "responses-target",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "bridge me"}],
            }
            response = await client.post(
                "/v1/messages",
                headers={"Authorization": "Bearer rt-test-key"},
                json=payload,
            )
            assert response.status_code == 200, response.text
            assert response.json()["content"][0]["text"] == "Responses bridge"

            async with client.stream(
                "POST",
                "/v1/messages",
                headers={"Authorization": "Bearer rt-test-key"},
                json={**payload, "stream": True},
            ) as streamed:
                assert streamed.status_code == 200, await streamed.aread()
                body = await streamed.aread()

        assert len(seen_requests) == 2
        request_payload = json.loads(seen_requests[0].content)
        assert request_payload["input"][0]["content"][0]["text"] == "bridge me"
        assert "messages" not in request_payload
        assert b"message_stop" in body
        assert b"streamed" in body
        assert [item.path for item in observations] == ["/responses", "/responses"]
        assert all(item.wire_surface == "openai_responses" for item in observations)
        assert result.health_manager.get_health_stats("rt-acct-1")["is_healthy"]
    finally:
        await result.runtime_manager.shutdown()
        await result.db.disconnect()
        await result.httpx_client.aclose()


@respx.mock
@pytest.mark.asyncio()
async def test_openai_chat_client_bridges_to_anthropic_messages_upstream(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The coordinator adapts Chat requests and streams to Messages."""
    monkeypatch.setenv("REAL_RUNTIME_KEY", "rt-test-key")
    observations: list[Any] = []
    model = ModelSpec(
        model_id="messages-target",
        protocol="anthropic",
        capabilities={
            "thinking": {
                "status": "supported",
                "native_protocols": ["openai"],
                "supported_efforts": ["none", "high"],
            }
        },
    )
    spec = RuntimeAppSpec(
        account_names=("rt-acct-1",),
        models=(model,),
        providers=(
            ProviderSpec(
                provider_id="messages-provider",
                protocols=("anthropic",),
                static_models=(model,),
                account_names=("rt-acct-1",),
                wire_surfaces={
                    "anthropic_messages": {
                        "path_template": "/messages",
                        "priority": 10,
                        "auth": {"mode": "api_key", "header": "x-api-key"},
                    }
                },
            ),
        ),
        outbound_observer=observations.append,
        wire_runtime_enabled=True,
    )
    result = await build_runtime_app(spec, tmp_path=tmp_path)
    try:
        seen_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_requests.append(request)
            if request.content and b'"stream":true' in request.content:
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=_anthropic_stream(),
                )
            return httpx.Response(200, json=_anthropic_cross_surface_success())

        respx.post(f"{UPSTREAM_BASE}/messages").mock(side_effect=handler)
        from httpx import ASGITransport

        transport = ASGITransport(app=result.application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            payload = {
                "model": "messages-target",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "bridge me"}],
                "reasoning_effort": "none",
            }
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer rt-test-key"},
                json=payload,
            )
            assert response.status_code == 200, response.text
            assert response.json()["choices"][0]["message"]["content"] == (
                "Messages bridge"
            )

            async with client.stream(
                "POST",
                "/v1/chat/completions",
                headers={"Authorization": "Bearer rt-test-key"},
                json={**payload, "stream": True},
            ) as streamed:
                assert streamed.status_code == 200, await streamed.aread()
                body = await streamed.aread()

        assert len(seen_requests) == 2
        request_payload = json.loads(seen_requests[0].content)
        assert request_payload["messages"][0]["content"] == "bridge me"
        assert "thinking" not in request_payload
        assert b"data: [DONE]" in body
        assert b"streamed" in body
        assert [item.path for item in observations] == ["/messages", "/messages"]
        assert all(item.wire_surface == "anthropic_messages" for item in observations)
        assert result.health_manager.get_health_stats("rt-acct-1")["is_healthy"]
    finally:
        await result.runtime_manager.shutdown()
        await result.db.disconnect()
        await result.httpx_client.aclose()


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
async def test_unhinted_known_model_relearns_after_weak_surface_rejection(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catalog knowledge makes weak model rejection negotiable."""
    monkeypatch.setenv("REAL_RUNTIME_KEY", "rt-test-key")
    observations: list[Any] = []
    spec = RuntimeAppSpec(
        account_names=("rt-acct-1",),
        models=(ModelSpec(model_id="unhinted-model", protocol="openai"),),
        providers=(
            ProviderSpec(
                provider_id="synthetic-wire-provider",
                protocols=("openai",),
                static_models=(
                    ModelSpec(model_id="unhinted-model", protocol="openai"),
                ),
                account_names=("rt-acct-1",),
                wire_surfaces=_UNHINTED_WIRE_SURFACES,
            ),
        ),
        outbound_observer=observations.append,
        wire_runtime_enabled=True,
    )
    result = await build_runtime_app(spec, tmp_path=tmp_path)
    try:
        assert (
            result.catalog.cache.get_provider_model_entry(
                "unhinted-model", "synthetic-wire-provider"
            )
            is not None
        )
        response_count = {"responses": 0, "chat": 0}

        def responses_handler(_request: httpx.Request) -> httpx.Response:
            response_count["responses"] += 1
            return httpx.Response(
                401,
                json={
                    "type": "error",
                    "error": {
                        "type": "ModelError",
                        "message": "Model unhinted-model is not supported",
                    },
                },
            )

        def chat_handler(_request: httpx.Request) -> httpx.Response:
            response_count["chat"] += 1
            return httpx.Response(200, json=_chat_success())

        respx.post(f"{UPSTREAM_BASE}/responses").mock(side_effect=responses_handler)
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=chat_handler)

        from httpx import ASGITransport

        transport = ASGITransport(app=result.application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            payload = {
                "model": "unhinted-model",
                "messages": [{"role": "user", "content": "hello"}],
            }
            first = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer rt-test-key"},
                json=payload,
            )
            assert first.status_code == 200, first.text
            assert response_count == {"responses": 1, "chat": 1}

            second = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer rt-test-key"},
                json=payload,
            )
            assert second.status_code == 200, second.text
            assert response_count == {"responses": 1, "chat": 2}

        assert [item.path for item in observations] == [
            "/responses",
            "/chat/completions",
            "/chat/completions",
        ]
        assert observations[0].status_code == 401
        assert observations[0].wire_surface == "openai_responses"
        assert observations[1].attempt_ordinal == 2
        assert observations[2].attempt_ordinal == 1
        assert all(item.account_id == "rt-acct-1" for item in observations)
        health = result.health_manager.get_health_stats("rt-acct-1")
        assert health["is_healthy"] is True
        assert health["disabled_reason"] == ""
    finally:
        await result.runtime_manager.shutdown()
        await result.db.disconnect()
        await result.httpx_client.aclose()


@respx.mock
@pytest.mark.asyncio()
async def test_unknown_model_not_found_does_not_enumerate_declared_surfaces(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strong absence remains model quarantine, not surface roulette."""
    monkeypatch.setenv("REAL_RUNTIME_KEY", "rt-test-key")
    observations: list[Any] = []
    spec = RuntimeAppSpec(
        account_names=("rt-acct-1",),
        models=(ModelSpec(model_id="unknown-model", protocol="openai"),),
        providers=(
            ProviderSpec(
                provider_id="synthetic-wire-provider",
                protocols=("openai",),
                static_models=(),
                account_names=("rt-acct-1",),
                wire_surfaces=_UNHINTED_WIRE_SURFACES,
            ),
        ),
        outbound_observer=observations.append,
        wire_runtime_enabled=True,
    )
    result = await build_runtime_app(spec, tmp_path=tmp_path)
    try:
        respx.post(f"{UPSTREAM_BASE}/responses").mock(
            return_value=httpx.Response(
                404,
                json={"error": {"message": "Model not found"}},
            )
        )
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(200, json=_chat_success())
        )

        from httpx import ASGITransport

        transport = ASGITransport(app=result.application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer rt-test-key"},
                json={
                    "model": "unknown-model",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
        assert response.status_code in {404, 502, 503, 504}
        assert [item.path for item in observations] == ["/responses"]
    finally:
        await result.runtime_manager.shutdown()
        await result.db.disconnect()
        await result.httpx_client.aclose()


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
