"""Provider thinking-control request-path body capture tests.

Proves through the real Eggpool ASGI endpoint that:
1. OpenCode Go MiniMax-M3 warn-drop mode sends a sanitized body
   with no unsupported thinking controls.
2. Native MiniMax effort contract preserves/maps accepted thinking
   controls in the captured upstream body.
3. Streaming and non-streaming request construction produce identical
   provider-control adaptation decisions.

Each test captures the actual body bytes sent to the mock upstream
and asserts on the wire content, not on internal adaptation results.
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Upstream response helpers
# ---------------------------------------------------------------------------

_OPENAI_SUCCESS_BODY = {
    "id": "chatcmpl-plan046",
    "object": "chat.completion",
    "created": 1700000000,
    "model": "MiniMax-M3",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello from upstream"},
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    },
}


def _openai_stream_chunks() -> list[bytes]:
    return [
        b'data: {"id":"chatcmpl-plan046","object":"chat.completion.chunk",'
        b'"choices":[{"index":0,"delta":{"role":"assistant","content":""},'
        b'"finish_reason":null}]}\n\n',
        b'data: {"choices":[{"index":0,"delta":{"content":"Hello"},'
        b'"finish_reason":null}]}\n\n',
        b'data: {"choices":[{"index":0,"delta":{"content":" from upstream"},'
        b'"finish_reason":null}]}\n\n',
        b'data: {"usage":{"prompt_tokens":10,"completion_tokens":5,'
        b'"total_tokens":15},"choices":[]}\n\n',
        b"data: [DONE]\n\n",
    ]


# ---------------------------------------------------------------------------
# Fixture: OpenCode Go MiniMax-M3 with warn_drop policy
# ---------------------------------------------------------------------------

_OPENCODE_GO_WARN_DROP_SPEC = RuntimeAppSpec(
    account_names=("rt-acct-1",),
    models=(
        ModelSpec(
            model_id="MiniMax-M3",
            protocol="openai",
            capabilities={
                "thinking": {
                    "status": "supported",
                    "control_contract": {
                        "mode": "fixed",
                        "source": "manual_override",
                    },
                },
            },
        ),
    ),
    providers=(
        ProviderSpec(
            provider_id="opencode-go",
            base_url=UPSTREAM_BASE,
            protocols=("openai", "anthropic"),
            static_models=(
                ModelSpec(
                    model_id="MiniMax-M3",
                    protocol="openai",
                    capabilities={
                        "thinking": {
                            "status": "supported",
                            "control_contract": {
                                "mode": "fixed",
                                "source": "manual_override",
                            },
                        },
                    },
                ),
            ),
            account_names=("rt-acct-1",),
        ),
    ),
    transcoder_overrides={
        "provider_control_policy": {
            "unsupported_control": "warn_drop",
        },
    },
)


@pytest_asyncio.fixture()
async def opencode_go_warn_drop_app(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[FastAPI, None]:
    monkeypatch.setenv("REAL_RUNTIME_KEY", "rt-test-key")
    result = await build_runtime_app(_OPENCODE_GO_WARN_DROP_SPEC, tmp_path=tmp_path)
    yield result.application
    await result.db.disconnect()
    await result.httpx_client.aclose()


# ---------------------------------------------------------------------------
# Fixture: Native MiniMax with effort contract
# ---------------------------------------------------------------------------

_NATIVE_MINIMAX_SPEC = RuntimeAppSpec(
    account_names=("rt-acct-1",),
    models=(
        ModelSpec(
            model_id="MiniMax-M3",
            protocol="anthropic",
            capabilities={
                "thinking": {
                    "status": "supported",
                    "control_contract": {
                        "mode": "effort",
                        "request_fields": ["thinking"],
                        "accepted_efforts": ["low", "medium", "high"],
                        "effort_aliases": {"med": "medium"},
                        "effort_to_budget_tokens": {
                            "low": 1024,
                            "medium": 4096,
                            "high": 16384,
                        },
                        "historical_reasoning_content": "accepted",
                        "source": "manual_override",
                    },
                },
            },
        ),
    ),
    providers=(
        ProviderSpec(
            provider_id="minimax",
            base_url=UPSTREAM_BASE,
            protocols=("anthropic",),
            static_models=(
                ModelSpec(
                    model_id="MiniMax-M3",
                    protocol="anthropic",
                    capabilities={
                        "thinking": {
                            "status": "supported",
                            "control_contract": {
                                "mode": "effort",
                                "request_fields": ["thinking"],
                                "accepted_efforts": ["low", "medium", "high"],
                                "effort_aliases": {"med": "medium"},
                                "effort_to_budget_tokens": {
                                    "low": 1024,
                                    "medium": 4096,
                                    "high": 16384,
                                },
                                "historical_reasoning_content": "accepted",
                                "source": "manual_override",
                            },
                        },
                    },
                ),
            ),
            account_names=("rt-acct-1",),
        ),
    ),
    transcoder_overrides={
        "provider_control_policy": {
            "unsupported_control": "reject",
        },
    },
)


@pytest_asyncio.fixture()
async def native_minimax_app(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[FastAPI, None]:
    monkeypatch.setenv("REAL_RUNTIME_KEY", "rt-test-key")
    result = await build_runtime_app(_NATIVE_MINIMAX_SPEC, tmp_path=tmp_path)
    yield result.application
    await result.db.disconnect()
    await result.httpx_client.aclose()


# ---------------------------------------------------------------------------
# Fixture: OpenCode Go MiniMax-M3 with reject policy (for parity test)
# ---------------------------------------------------------------------------

_OPENCODE_GO_REJECT_SPEC = RuntimeAppSpec(
    account_names=("rt-acct-1",),
    models=(
        ModelSpec(
            model_id="MiniMax-M3",
            protocol="openai",
            capabilities={
                "thinking": {
                    "status": "supported",
                    "control_contract": {
                        "mode": "fixed",
                        "source": "manual_override",
                    },
                },
            },
        ),
    ),
    providers=(
        ProviderSpec(
            provider_id="opencode-go",
            base_url=UPSTREAM_BASE,
            protocols=("openai", "anthropic"),
            static_models=(
                ModelSpec(
                    model_id="MiniMax-M3",
                    protocol="openai",
                    capabilities={
                        "thinking": {
                            "status": "supported",
                            "control_contract": {
                                "mode": "fixed",
                                "source": "manual_override",
                            },
                        },
                    },
                ),
            ),
            account_names=("rt-acct-1",),
        ),
    ),
    transcoder_overrides={
        "provider_control_policy": {
            "unsupported_control": "reject",
        },
    },
)

# Canonical OpenCode Go Muse Spark model with an explicit provider capability
# declaration. Production no longer infers this contract from the host or
# model name, so the fixture supplies the same verified facts a provider
# catalog or operator override would provide.
_MUSE_SPARK_THINKING_CAPABILITY = {
    "thinking": {
        "status": "supported",
        "source": "provider_catalog",
        "native_protocols": ["openai"],
        "supported_efforts": ["minimal", "low", "medium", "high", "xhigh"],
        "effort_to_budget_tokens": {
            "minimal": 1024,
            "low": 1024,
            "medium": 4096,
            "high": 16384,
            "xhigh": 24576,
        },
    },
}

_MUSE_SPARK_SPEC = RuntimeAppSpec(
    account_names=("rt-acct-1",),
    models=(
        ModelSpec(
            model_id="muse-spark-1.2-contributor",
            protocol="openai",
            capabilities=_MUSE_SPARK_THINKING_CAPABILITY,
        ),
    ),
    providers=(
        ProviderSpec(
            provider_id="opencode-go",
            base_url="https://opencode.ai/zen/go/v1",
            protocols=("openai",),
            static_models=(
                ModelSpec(
                    model_id="muse-spark-1.2-contributor",
                    protocol="openai",
                    capabilities=_MUSE_SPARK_THINKING_CAPABILITY,
                ),
            ),
            account_names=("rt-acct-1",),
        ),
    ),
)


@pytest_asyncio.fixture()
async def opencode_go_reject_app(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[FastAPI, None]:
    monkeypatch.setenv("REAL_RUNTIME_KEY", "rt-test-key")
    result = await build_runtime_app(_OPENCODE_GO_REJECT_SPEC, tmp_path=tmp_path)
    yield result.application
    await result.db.disconnect()
    await result.httpx_client.aclose()


@pytest_asyncio.fixture()
async def muse_spark_app(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[FastAPI, None]:
    monkeypatch.setenv("REAL_RUNTIME_KEY", "rt-test-key")
    result = await build_runtime_app(_MUSE_SPARK_SPEC, tmp_path=tmp_path)
    yield result.application
    await result.db.disconnect()
    await result.httpx_client.aclose()


# ---------------------------------------------------------------------------
# Test: OpenCode Go warn-drop body capture
# ---------------------------------------------------------------------------


class TestMuseSparkThinkingControl:
    """Muse Spark xhigh routes through the OpenAI-family endpoint."""

    @respx.mock
    @pytest.mark.asyncio()
    async def test_xhigh_is_routable_and_transcoded(
        self,
        muse_spark_app: FastAPI,
    ) -> None:
        from httpx import ASGITransport

        captured_bodies: list[bytes] = []

        async def _capture_handler(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(request.content)
            return httpx.Response(
                200,
                json={
                    "id": "chat-muse-spark",
                    "object": "chat.completion",
                    "model": "muse-spark-1.2-contributor",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "pong"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 1,
                        "total_tokens": 6,
                    },
                },
            )

        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=_capture_handler
        )

        async with httpx.AsyncClient(
            transport=ASGITransport(app=muse_spark_app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer rt-test-key"},
                json={
                    "model": "muse-spark-1.2-contributor",
                    "messages": [{"role": "user", "content": "Reply pong"}],
                    "max_tokens": 32768,
                    "reasoning_effort": "xhigh",
                },
            )

        assert response.status_code == 200, response.text
        assert response.json()["choices"][0]["message"]["content"] == "pong"
        assert len(captured_bodies) == 1
        upstream_body = json.loads(captured_bodies[0])
        assert upstream_body.get("reasoning_effort") == "xhigh", upstream_body


class TestWarnDropBodyCapture:
    """OpenCode Go MiniMax-M3 warn-drop: captured upstream body has no
    thinking controls."""

    @respx.mock
    @pytest.mark.asyncio()
    async def test_reasoning_effort_stripped_from_body(
        self,
        opencode_go_warn_drop_app: FastAPI,
    ) -> None:
        """reasoning_effort is removed before upstream dispatch."""
        from httpx import ASGITransport

        transport = ASGITransport(app=opencode_go_warn_drop_app)
        captured_bodies: list[bytes] = []

        async def _capture_handler(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(request.content)
            return httpx.Response(200, json=_OPENAI_SUCCESS_BODY)

        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=_capture_handler,
        )

        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer rt-test-key"},
                json={
                    "model": "MiniMax-M3",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "reasoning_effort": "high",
                },
            )

        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )
        assert len(captured_bodies) == 1

        body = json.loads(captured_bodies[0])
        assert "reasoning_effort" not in body, (
            "reasoning_effort must be stripped from upstream body"
        )
        assert body.get("model") == "MiniMax-M3"
        assert "messages" in body

    @respx.mock
    @pytest.mark.asyncio()
    async def test_thinking_block_stripped_from_body(
        self,
        opencode_go_warn_drop_app: FastAPI,
    ) -> None:
        """thinking object with type/effort/budget is removed."""
        from httpx import ASGITransport

        transport = ASGITransport(app=opencode_go_warn_drop_app)
        captured_bodies: list[bytes] = []

        async def _capture_handler(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(request.content)
            return httpx.Response(200, json=_OPENAI_SUCCESS_BODY)

        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=_capture_handler,
        )

        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer rt-test-key"},
                json={
                    "model": "MiniMax-M3",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "reasoning_effort": "high",
                    "thinking": {
                        "type": "enabled",
                        "effort": "high",
                        "budget_tokens": 4096,
                    },
                },
            )

        assert resp.status_code == 200
        assert len(captured_bodies) == 1

        body = json.loads(captured_bodies[0])
        assert "reasoning_effort" not in body
        assert "thinking" not in body, (
            "thinking block must be stripped from upstream body"
        )
        assert "messages" in body

    @respx.mock
    @pytest.mark.asyncio()
    async def test_unrelated_fields_preserved(
        self,
        opencode_go_warn_drop_app: FastAPI,
    ) -> None:
        """Fields unrelated to thinking controls are preserved."""
        from httpx import ASGITransport

        transport = ASGITransport(app=opencode_go_warn_drop_app)
        captured_bodies: list[bytes] = []

        async def _capture_handler(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(request.content)
            return httpx.Response(200, json=_OPENAI_SUCCESS_BODY)

        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=_capture_handler,
        )

        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer rt-test-key"},
                json={
                    "model": "MiniMax-M3",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "reasoning_effort": "high",
                    "temperature": 0.7,
                    "max_tokens": 100,
                },
            )

        assert resp.status_code == 200
        assert len(captured_bodies) == 1

        body = json.loads(captured_bodies[0])
        assert body.get("temperature") == 0.7, "temperature must be preserved"
        assert body.get("max_tokens") == 100, "max_tokens must be preserved"
        assert "reasoning_effort" not in body


# ---------------------------------------------------------------------------
# Test: Native MiniMax preservation body capture
# ---------------------------------------------------------------------------


class TestNativeMiniMaxPreservation:
    """Native MiniMax effort contract: accepted thinking controls are
    preserved in the captured upstream body."""

    @respx.mock
    @pytest.mark.asyncio()
    async def test_accepted_effort_preserved(
        self,
        native_minimax_app: FastAPI,
    ) -> None:
        """Accepted reasoning_effort passes through to upstream."""
        from httpx import ASGITransport

        transport = ASGITransport(app=native_minimax_app)
        captured_bodies: list[bytes] = []

        async def _capture_handler(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(request.content)
            return httpx.Response(200, json=_OPENAI_SUCCESS_BODY)

        respx.post(f"{UPSTREAM_BASE}/messages").mock(
            side_effect=_capture_handler,
        )

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
                    "model": "MiniMax-M3",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "thinking": {"type": "enabled", "effort": "high"},
                },
            )

        assert resp.status_code == 200
        assert len(captured_bodies) == 1

        body = json.loads(captured_bodies[0])
        thinking = body.get("thinking")
        assert isinstance(thinking, dict), (
            "thinking block must be preserved in upstream body"
        )
        assert thinking.get("effort") == "high", (
            "accepted effort value must be preserved"
        )

    @respx.mock
    @pytest.mark.asyncio()
    async def test_alias_mapped_in_body(
        self,
        native_minimax_app: FastAPI,
    ) -> None:
        """Top-level reasoning_effort alias 'med' is mapped to 'medium'."""
        from httpx import ASGITransport

        transport = ASGITransport(app=native_minimax_app)
        captured_bodies: list[bytes] = []

        async def _capture_handler(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(request.content)
            return httpx.Response(200, json=_OPENAI_SUCCESS_BODY)

        respx.post(f"{UPSTREAM_BASE}/messages").mock(
            side_effect=_capture_handler,
        )

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
                    "model": "MiniMax-M3",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "reasoning_effort": "med",
                },
            )

        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )
        assert len(captured_bodies) == 1

        body = json.loads(captured_bodies[0])
        assert body.get("reasoning_effort") == "medium", (
            f"alias 'med' must be mapped to 'medium', got: "
            f"{body.get('reasoning_effort')}"
        )

    @respx.mock
    @pytest.mark.asyncio()
    async def test_unsupported_effort_rejected_no_upstream(
        self,
        native_minimax_app: FastAPI,
    ) -> None:
        """Unsupported reasoning_effort raises CapabilityError; no upstream
        request is dispatched."""
        from httpx import ASGITransport

        transport = ASGITransport(app=native_minimax_app)
        captured_bodies: list[bytes] = []

        async def _capture_handler(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(request.content)
            return httpx.Response(200, json=_OPENAI_SUCCESS_BODY)

        respx.post(f"{UPSTREAM_BASE}/messages").mock(
            side_effect=_capture_handler,
        )

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
                    "model": "MiniMax-M3",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "reasoning_effort": "xhigh",
                },
            )

        assert resp.status_code == 400
        assert len(captured_bodies) == 0, "reject mode must not dispatch upstream"


# ---------------------------------------------------------------------------
# Test: Streaming / non-streaming parity
# ---------------------------------------------------------------------------


class TestStreamingNonStreamingParity:
    """Streaming and non-streaming requests produce identical
    provider-control adaptation decisions."""

    @respx.mock
    @pytest.mark.asyncio()
    async def test_opencode_go_reject_same_decision_both_modes(
        self,
        opencode_go_reject_app: FastAPI,
    ) -> None:
        """Both streaming and non-streaming reject unsupported effort
        with HTTP 400 and zero upstream requests."""
        from httpx import ASGITransport

        transport = ASGITransport(app=opencode_go_reject_app)
        upstream_calls: list[str] = []

        async def _capture_handler(request: httpx.Request) -> httpx.Response:
            upstream_calls.append(str(request.url))
            return httpx.Response(200, json=_OPENAI_SUCCESS_BODY)

        async def _capture_stream_handler(
            request: httpx.Request,
        ) -> httpx.Response:
            upstream_calls.append(str(request.url))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_openai_stream_chunks(),
            )

        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=_capture_handler,
        )

        request_body = {
            "model": "MiniMax-M3",
            "messages": [{"role": "user", "content": "Hello"}],
            "reasoning_effort": "high",
        }

        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            # Non-streaming: should get 400
            resp_non_stream = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer rt-test-key"},
                json={**request_body, "stream": False},
            )

            # Streaming: should also get 400 (same decision)
            resp_stream = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer rt-test-key"},
                json={**request_body, "stream": True},
            )

        assert resp_non_stream.status_code == 400
        assert resp_stream.status_code == 400
        assert len(upstream_calls) == 0, (
            "Both modes must produce zero upstream calls for rejected controls"
        )

    @respx.mock
    @pytest.mark.asyncio()
    async def test_opencode_go_warn_drop_same_body_both_modes(
        self,
        opencode_go_warn_drop_app: FastAPI,
    ) -> None:
        """Both streaming and non-streaming produce the same sanitized
        body when controls are warn-dropped."""
        from httpx import ASGITransport

        transport = ASGITransport(app=opencode_go_warn_drop_app)
        captured_bodies: list[bytes] = []

        async def _capture_handler(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(request.content)
            return httpx.Response(200, json=_OPENAI_SUCCESS_BODY)

        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=_capture_handler,
        )

        request_body = {
            "model": "MiniMax-M3",
            "messages": [{"role": "user", "content": "Hello"}],
            "reasoning_effort": "high",
        }

        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            # Non-streaming
            resp_ns = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer rt-test-key"},
                json={**request_body, "stream": False},
            )
            # Streaming
            resp_s = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer rt-test-key"},
                json={**request_body, "stream": True},
            )

        assert resp_ns.status_code == 200
        assert resp_s.status_code == 200
        assert len(captured_bodies) == 2

        body_ns = json.loads(captured_bodies[0])
        body_s = json.loads(captured_bodies[1])

        # Both must have the same sanitized shape (no thinking controls)
        assert "reasoning_effort" not in body_ns
        assert "reasoning_effort" not in body_s
        # Both must preserve the same unrelated fields
        assert body_ns.get("model") == body_s.get("model") == "MiniMax-M3"
        assert body_ns.get("messages") == body_s.get("messages")
