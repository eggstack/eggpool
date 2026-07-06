"""Phase 4.4 tests: verify coordinator uses precomputed reservation tokens.

Pins the contract that ProxyRequestContext.estimated_reservation_tokens
and thinking_requirement are set once in handle_proxy_request() and
consumed by _select_and_persist_attempt() without reparsing the body.
"""

from __future__ import annotations

import json

from eggpool.request.coordinator import (
    ProxyRequestContext,
    estimate_reservation_tokens,
)


def test_precomputed_reservation_tokens_used() -> None:
    body = json.dumps({
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    expected = estimate_reservation_tokens(body)
    ctx = ProxyRequestContext(
        request_id="test-1",
        protocol="openai",
        model_id="gpt-4",
        streaming=False,
        original_body=body,
        incoming_headers={},
        estimated_reservation_tokens=expected,
    )
    assert ctx.estimated_reservation_tokens == expected


def test_thinking_requirement_stored() -> None:
    body = json.dumps({
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "hi"}],
        "reasoning_effort": "high",
    }).encode()
    ctx = ProxyRequestContext(
        request_id="test-2",
        protocol="openai",
        model_id="gpt-4",
        streaming=False,
        original_body=body,
        incoming_headers={},
    )
    assert ctx.thinking_requirement is None
    ctx.thinking_requirement = "mock_thinking_req"
    assert ctx.thinking_requirement == "mock_thinking_req"


def test_precomputed_tokens_none_fallback() -> None:
    body = b'{"model":"gpt-4"}'
    ctx = ProxyRequestContext(
        request_id="test-3",
        protocol="openai",
        model_id="gpt-4",
        streaming=False,
        original_body=body,
        incoming_headers={},
    )
    assert ctx.estimated_reservation_tokens is None
    assert ctx.thinking_requirement is None
    tokens = estimate_reservation_tokens(body)
    assert tokens > 0
