"""Performance baseline benchmarks for the EggPool request path.

Captures behavioral snapshots and timing metrics for core request-path
components using mocked upstreams.  Run with::

    pytest tests/perf/test_perf_baseline.py -m perf_baseline -v
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx

from eggpool.routing.eligibility import get_eligible_accounts
from eggpool.transcoder.anthropic_to_openai import AnthropicToOpenAI
from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.openai_to_anthropic import OpenAIToAnthropic
from eggpool.transcoder.segmentation import segment_request

pytestmark = pytest.mark.performance

if TYPE_CHECKING:
    from eggpool.request.coordinator import RequestCoordinator

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

UPSTREAM_URL = "https://perf-test-upstream.example.com"


def _openai_payload(
    model: str = "gpt-4",
    **extras: Any,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "Hello, world!"}],
    }
    base.update(extras)
    return base


def _anthropic_payload(
    model: str = "claude-3-sonnet-20240229",
    **extras: Any,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "model": model,
        "max_tokens": 128,
        "messages": [{"role": "user", "content": "Hello, world!"}],
    }
    base.update(extras)
    return base


def _make_context(
    *,
    request_id: str,
    protocol: str,
    model_id: str,
    body: dict[str, Any],
    streaming: bool = False,
) -> Any:
    from eggpool.request.coordinator import ProxyRequestContext

    return ProxyRequestContext(
        request_id=request_id,
        protocol=protocol,
        model_id=model_id,
        streaming=streaming,
        original_body=json.dumps(body).encode(),
        incoming_headers={"content-type": "application/json"},
    )


async def _noop_upstream(
    request: httpx.Request,
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-perf",
            "object": "chat.completion",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "OK",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        },
    )


async def _noop_anthropic_upstream(
    request: httpx.Request,
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg-perf",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "OK"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )


async def _fail_upstream(
    request: httpx.Request,
) -> httpx.Response:
    return httpx.Response(
        502,
        json={
            "error": {
                "message": "Bad gateway",
                "type": "server_error",
                "code": "bad_gateway",
            }
        },
    )


def _emit_snapshot(
    *,
    test_name: str,
    wall_ms: float,
    selected_account: str | None = None,
    attempt_count: int = 1,
    upstream_url: str | None = None,
    status_code: int | None = None,
    extras: dict[str, Any] | None = None,
) -> None:
    """Print a structured behavioral snapshot for diagnostic output."""
    snapshot: dict[str, Any] = {
        "test": test_name,
        "wall_ms": round(wall_ms, 3),
    }
    if selected_account is not None:
        snapshot["selected_account"] = selected_account
    snapshot["attempt_count"] = attempt_count
    if upstream_url is not None:
        snapshot["upstream_url"] = upstream_url
    if status_code is not None:
        snapshot["status_code"] = status_code
    if extras:
        snapshot.update(extras)
    print(f"\n  [PERF] {json.dumps(snapshot, indent=2)}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.perf_baseline
async def test_native_openai_nonstreaming_request(
    perf_coordinator: RequestCoordinator,
) -> None:
    """Route a native OpenAI chat completion through the coordinator."""
    payload = _openai_payload()
    context = _make_context(
        request_id="perf-openai-001",
        protocol="openai",
        model_id="gpt-4",
        body=payload,
    )

    t0 = time.perf_counter()
    async with respx.mock(base_url=UPSTREAM_URL) as respx_router:
        respx_router.route(
            method="POST",
            path="/chat/completions",
        ).mock(side_effect=_noop_upstream)
        response = await perf_coordinator.execute(context)
    wall_ms = (time.perf_counter() - t0) * 1000

    _emit_snapshot(
        test_name="native_openai_nonstreaming",
        wall_ms=wall_ms,
        selected_account=context.attempted_accounts.pop()
        if context.attempted_accounts
        else None,
        attempt_count=response.attempt_count,
        status_code=response.status_code,
        extras={"latency_ms": response.latency_ms},
    )

    assert response.status_code == 200
    assert response.body is not None
    body = json.loads(response.body)
    assert body["choices"][0]["message"]["content"] == "OK"


@pytest.mark.asyncio
@pytest.mark.perf_baseline
async def test_native_anthropic_nonstreaming_request(
    perf_coordinator: RequestCoordinator,
) -> None:
    """Route a native Anthropic messages API request."""
    payload = _anthropic_payload()
    context = _make_context(
        request_id="perf-anthropic-001",
        protocol="anthropic",
        model_id="claude-3-sonnet-20240229",
        body=payload,
    )

    t0 = time.perf_counter()
    async with respx.mock(base_url=UPSTREAM_URL) as respx_router:
        respx_router.route(
            method="POST",
            path="/messages",
        ).mock(side_effect=_noop_anthropic_upstream)
        response = await perf_coordinator.execute(context)
    wall_ms = (time.perf_counter() - t0) * 1000

    _emit_snapshot(
        test_name="native_anthropic_nonstreaming",
        wall_ms=wall_ms,
        selected_account=context.attempted_accounts.pop()
        if context.attempted_accounts
        else None,
        attempt_count=response.attempt_count,
        status_code=response.status_code,
        extras={"latency_ms": response.latency_ms},
    )

    assert response.status_code == 200
    assert response.body is not None
    body = json.loads(response.body)
    assert body["content"][0]["text"] == "OK"


@pytest.mark.asyncio
@pytest.mark.perf_baseline
async def test_transcode_openai_to_anthropic(
    perf_coordinator: RequestCoordinator,
) -> None:
    """OpenAI client request routed to Anthropic upstream."""
    ctx = TranscodeContext(
        request_id="perf-xcode-001",
        client_protocol="openai",
        upstream_protocol="anthropic",
    )
    transcoder = OpenAIToAnthropic()

    openai_body = _openai_payload(
        messages=[
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Say hello."},
        ],
    )

    t0 = time.perf_counter()
    upstream_body, warnings = transcoder.encode_request(
        openai_body,
        ctx,
    )
    encode_ms = (time.perf_counter() - t0) * 1000

    assert upstream_body["model"] == "gpt-4"
    assert upstream_body["max_tokens"] == 4096
    assert "messages" in upstream_body

    decoded_response = {
        "id": "msg-xcode-001",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "Hello!"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": 3},
    }

    t1 = time.perf_counter()
    client_response, decode_warnings = transcoder.decode_response(
        decoded_response,
        ctx,
    )
    decode_ms = (time.perf_counter() - t1) * 1000

    assert client_response["choices"][0]["message"]["content"] == "Hello!"

    _emit_snapshot(
        test_name="transcode_openai_to_anthropic",
        wall_ms=encode_ms + decode_ms,
        extras={
            "encode_ms": round(encode_ms, 3),
            "decode_ms": round(decode_ms, 3),
            "loss_warnings": len(warnings),
        },
    )


@pytest.mark.asyncio
@pytest.mark.perf_baseline
async def test_transcode_anthropic_to_openai(
    perf_coordinator: RequestCoordinator,
) -> None:
    """Anthropic client request routed to OpenAI upstream."""
    ctx = TranscodeContext(
        request_id="perf-xcode-002",
        client_protocol="anthropic",
        upstream_protocol="openai",
    )
    transcoder = AnthropicToOpenAI()

    anthropic_body = _anthropic_payload(
        messages=[{"role": "user", "content": "Say hello."}],
    )

    t0 = time.perf_counter()
    upstream_body, warnings = transcoder.encode_request(
        anthropic_body,
        ctx,
    )
    encode_ms = (time.perf_counter() - t0) * 1000

    assert "model" in upstream_body
    assert "messages" in upstream_body

    decoded_response = {
        "id": "chatcmpl-xcode-002",
        "object": "chat.completion",
        "model": "gpt-4",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Hello!",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 8,
            "completion_tokens": 3,
            "total_tokens": 11,
        },
    }

    t1 = time.perf_counter()
    client_response, decode_warnings = transcoder.decode_response(
        decoded_response,
        ctx,
    )
    decode_ms = (time.perf_counter() - t1) * 1000

    assert client_response["content"][0]["text"] == "Hello!"

    _emit_snapshot(
        test_name="transcode_anthropic_to_openai",
        wall_ms=encode_ms + decode_ms,
        extras={
            "encode_ms": round(encode_ms, 3),
            "decode_ms": round(decode_ms, 3),
            "loss_warnings": len(warnings),
        },
    )


@pytest.mark.asyncio
@pytest.mark.perf_baseline
async def test_segmentation_latency(
    perf_coordinator: RequestCoordinator,
) -> None:
    """Measure segmentation overhead for various payload sizes."""
    small_payload = _openai_payload()
    medium_payload = _openai_payload(
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            *[
                {"role": "user", "content": f"Turn {i}: " + "x" * 500}
                for i in range(10)
            ],
        ],
    )
    large_payload = _openai_payload(
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            *[
                {"role": "user", "content": f"Turn {i}: " + "x" * 2000}
                for i in range(50)
            ],
        ],
    )

    results: dict[str, dict[str, Any]] = {}
    for label, payload in [
        ("small", small_payload),
        ("medium", medium_payload),
        ("large", large_payload),
    ]:
        t0 = time.perf_counter()
        result = segment_request(payload, protocol="openai")
        elapsed_ms = (time.perf_counter() - t0) * 1000
        results[label] = {
            "elapsed_ms": round(elapsed_ms, 3),
            "segment_count": len(result.segments),
            "status": result.status.value,
        }

    _emit_snapshot(
        test_name="segmentation_latency",
        wall_ms=sum(r["elapsed_ms"] for r in results.values()),
        extras={"payloads": results},
    )

    assert results["small"]["segment_count"] > 0
    assert results["medium"]["segment_count"] > 0
    assert results["large"]["segment_count"] > 0


@pytest.mark.asyncio
@pytest.mark.perf_baseline
async def test_routing_eligibility_latency(
    perf_coordinator: RequestCoordinator,
) -> None:
    """Measure routing eligibility computation time."""
    from eggpool.accounts.state import AccountRuntimeState

    states = [
        AccountRuntimeState(
            name="perf-acct",
            enabled=True,
            health_state="healthy",
        ),
    ]

    t0 = time.perf_counter()
    eligible = get_eligible_accounts(
        states,
        model_id="gpt-4",
        catalog=perf_coordinator._catalog.cache,
        health_manager=perf_coordinator._health_manager,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000

    _emit_snapshot(
        test_name="routing_eligibility",
        wall_ms=elapsed_ms,
        extras={
            "eligible_count": len(eligible),
            "state_count": len(states),
        },
    )

    assert len(eligible) == 1
    assert eligible[0].name == "perf-acct"


@pytest.mark.asyncio
@pytest.mark.perf_baseline
async def test_retryable_failure_and_failover(
    perf_coordinator: RequestCoordinator,
) -> None:
    """Verify retry behavior on upstream failure."""
    payload = _openai_payload()
    context = _make_context(
        request_id="perf-retry-001",
        protocol="openai",
        model_id="gpt-4",
        body=payload,
    )

    t0 = time.perf_counter()
    async with respx.mock(base_url=UPSTREAM_URL) as respx_router:
        respx_router.route(
            method="POST",
            path="/chat/completions",
        ).mock(side_effect=_fail_upstream)
        response = await perf_coordinator.execute(context)
    wall_ms = (time.perf_counter() - t0) * 1000

    _emit_snapshot(
        test_name="retryable_failure_and_failover",
        wall_ms=wall_ms,
        attempt_count=response.attempt_count,
        status_code=response.status_code,
        extras={"latency_ms": response.latency_ms},
    )

    assert response.status_code == 502


@pytest.mark.asyncio
@pytest.mark.perf_baseline
async def test_thinking_request_supported(
    perf_coordinator: RequestCoordinator,
) -> None:
    """Thinking/reasoning request with supported capability."""
    from eggpool.transcoder.policy import TranscoderFeatures

    ctx = TranscodeContext(
        request_id="perf-think-001",
        client_protocol="openai",
        upstream_protocol="anthropic",
    )
    transcoder = OpenAIToAnthropic()

    openai_body = _openai_payload(
        messages=[{"role": "user", "content": "Think step by step."}],
        reasoning_effort="high",
    )

    features = TranscoderFeatures(thinking=True)

    t0 = time.perf_counter()
    upstream_body, warnings = transcoder.encode_request(
        openai_body,
        ctx,
        features=features,
    )
    encode_ms = (time.perf_counter() - t0) * 1000

    assert upstream_body.get("thinking") is not None
    assert upstream_body["thinking"]["type"] == "enabled"
    assert "reasoning_effort" not in upstream_body

    anthropic_response = {
        "id": "msg-think-001",
        "type": "message",
        "role": "assistant",
        "content": [
            {
                "type": "thinking",
                "thinking": "Let me reason through this...",
                "signature": "sig",
            },
            {"type": "text", "text": "The answer is 42."},
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 30},
    }

    t1 = time.perf_counter()
    client_response, decode_warnings = transcoder.decode_response(
        anthropic_response,
        ctx,
        features=features,
    )
    decode_ms = (time.perf_counter() - t1) * 1000

    assert (
        client_response["choices"][0]["message"]["reasoning_content"]
        == "Let me reason through this..."
    )
    assert client_response["choices"][0]["message"]["content"] == "The answer is 42."

    _emit_snapshot(
        test_name="thinking_request_supported",
        wall_ms=encode_ms + decode_ms,
        extras={
            "encode_ms": round(encode_ms, 3),
            "decode_ms": round(decode_ms, 3),
            "thinking_preserved": True,
        },
    )
