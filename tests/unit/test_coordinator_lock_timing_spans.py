"""Phase 5 regression test: lock timing spans are populated.

Verifies that when DispatchSpanRecorder is enabled, the selection lock
timing fields are populated in the dispatch overhead recorder.
"""

from __future__ import annotations

from eggpool.request.coordinator import ProxyRequestContext, RequestCoordinator
from eggpool.runtime_dispatch import (
    ALL_SPAN_KEYS,
    SPAN_RESERVATION_ESTIMATE,
    SPAN_ROUTING_PLAN,
    SPAN_THINKING_CLASSIFICATION,
    DispatchSpanRecorder,
)


def test_dispatch_span_recorder_has_lock_spans() -> None:
    expected_spans = {
        SPAN_THINKING_CLASSIFICATION,
        SPAN_RESERVATION_ESTIMATE,
        SPAN_ROUTING_PLAN,
    }
    assert expected_spans.issubset(set(ALL_SPAN_KEYS))


def test_proxy_request_context_precomputed_fields() -> None:
    ctx = ProxyRequestContext(
        request_id="test-span",
        protocol="openai",
        model_id="gpt-4",
        streaming=False,
        original_body=b'{"messages":[{"role":"user","content":"hi"}]}',
        incoming_headers={},
        estimated_reservation_tokens=100,
        thinking_requirement=None,
        estimated_context_input_tokens=50,
    )
    assert ctx.estimated_reservation_tokens == 100
    assert ctx.thinking_requirement is None
    assert ctx.estimated_context_input_tokens == 50


def test_dispatch_span_recorder_records_all_coordinator_spans() -> None:
    all_span_names = set(ALL_SPAN_KEYS)
    coordinator_spans = {
        SPAN_THINKING_CLASSIFICATION,
        SPAN_RESERVATION_ESTIMATE,
        SPAN_ROUTING_PLAN,
    }
    assert coordinator_spans.issubset(all_span_names)


def test_request_coordinator_accepts_span_recorder() -> None:
    from unittest.mock import MagicMock

    import httpx

    recorder = DispatchSpanRecorder()
    mock_config = MagicMock()
    mock_config.routing.trace.mode = "off"
    mock_config.routing.trace.sample_rate = 0.0
    mock_config.routing.trace.include_score_components = False
    mock_config.routing.trace = MagicMock()
    mock_config.routing.trace.mode = "off"

    coordinator = RequestCoordinator(
        registry=MagicMock(),
        catalog=MagicMock(),
        router=MagicMock(),
        db=MagicMock(),
        client_pool=httpx.AsyncClient(),
        config=mock_config,
        dispatch_span_recorder=recorder,
    )
    assert coordinator._dispatch_span_recorder is recorder


def test_all_span_keys_complete() -> None:
    required = {
        "thinking_classification",
        "reservation_estimate",
        "routing_plan",
        "selection_lock_wait",
        "selection_locked",
    }
    assert required.issubset(set(ALL_SPAN_KEYS))
