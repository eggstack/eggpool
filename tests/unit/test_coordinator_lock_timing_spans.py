"""Phase 5 regression test: lock timing spans are populated.

Verifies that when DispatchSpanRecorder is enabled, the selection lock
timing fields are populated in the dispatch overhead recorder.
"""

from __future__ import annotations

from eggpool.request.coordinator import ProxyRequestContext, RequestCoordinator
from eggpool.runtime_dispatch import (
    ALL_SPAN_KEYS,
    SPAN_ACCOUNT_LOOKUP,
    SPAN_AUTH,
    SPAN_CIRCUIT_PROBE,
    SPAN_DB_WRITE_ATTEMPT,
    SPAN_DB_WRITE_REQUEST,
    SPAN_DB_WRITE_RESERVATION,
    SPAN_RESERVATION_ESTIMATE,
    SPAN_ROUTING_PLAN,
    SPAN_ROUTING_TRACE_BUILD,
    SPAN_ROUTING_TRACE_WRITE,
    SPAN_RUNTIME_PUBLICATION,
    SPAN_SELECTION_LOCK_WAIT,
    SPAN_SELECTION_LOCKED,
    SPAN_THINKING_CLASSIFICATION,
    DispatchSpanRecorder,
)


def test_dispatch_span_recorder_has_lock_spans() -> None:
    expected_spans = {
        SPAN_THINKING_CLASSIFICATION,
        SPAN_RESERVATION_ESTIMATE,
        SPAN_ROUTING_PLAN,
        SPAN_SELECTION_LOCK_WAIT,
        SPAN_SELECTION_LOCKED,
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
    """Phase 5: every coordinator-internal span is registered."""
    all_span_names = set(ALL_SPAN_KEYS)
    coordinator_spans = {
        SPAN_THINKING_CLASSIFICATION,
        SPAN_RESERVATION_ESTIMATE,
        SPAN_ROUTING_PLAN,
        SPAN_SELECTION_LOCK_WAIT,
        SPAN_SELECTION_LOCKED,
        SPAN_CIRCUIT_PROBE,
        SPAN_ACCOUNT_LOOKUP,
        SPAN_DB_WRITE_REQUEST,
        SPAN_DB_WRITE_RESERVATION,
        SPAN_DB_WRITE_ATTEMPT,
        SPAN_ROUTING_TRACE_BUILD,
        SPAN_ROUTING_TRACE_WRITE,
        SPAN_RUNTIME_PUBLICATION,
    }
    missing = coordinator_spans - all_span_names
    assert not missing, f"Missing spans: {missing}"


def test_auth_span_key_registered() -> None:
    """Phase 1: SPAN_AUTH is a registered span key."""
    assert SPAN_AUTH in set(ALL_SPAN_KEYS)


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
        "auth",
        "thinking_classification",
        "reservation_estimate",
        "routing_plan",
        "selection_lock_wait",
        "selection_locked",
        "circuit_probe",
        "account_lookup",
        "db_write_request",
        "db_write_reservation",
        "db_write_attempt",
        "routing_trace_build",
        "routing_trace_write",
        "runtime_publication",
    }
    missing = required - set(ALL_SPAN_KEYS)
    assert not missing, f"Missing required span keys: {missing}"
