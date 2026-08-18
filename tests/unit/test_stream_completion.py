"""Protocol-aware clean EOF decisions."""

from __future__ import annotations

from eggpool.proxy.sse_observer import IncrementalSSEObserver
from eggpool.request.stream_completion import classify_stream_eof


def test_openai_done_is_retained_when_fragmented() -> None:
    observer = IncrementalSSEObserver("openai")
    payload = b'data: {"choices": [{"delta": {"content": "x"}}]}\n\n'
    stream = payload + b"data: [DONE]\n\n"
    for byte in stream:
        observer.observe(bytes([byte]))
    observer.flush()

    snapshot = observer.completion_snapshot
    assert snapshot.saw_terminal_event
    assert snapshot.terminal_kind == "openai_done"
    assert (
        classify_stream_eof(
            protocol="openai",
            policy="strict",
            snapshot=snapshot,
            downstream_started=True,
        ).classification
        == "complete"
    )


def test_anthropic_message_stop_is_retained() -> None:
    observer = IncrementalSSEObserver("anthropic")
    stream = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    for byte in stream:
        observer.observe(bytes([byte]))
    observer.flush()

    assert observer.completion_snapshot.terminal_kind == "anthropic_message_stop"


def test_payload_without_terminal_is_premature_after_response_handoff() -> None:
    observer = IncrementalSSEObserver("openai")
    observer.observe(b'data: {"choices": []}\n\n')
    observer.flush()

    decision = classify_stream_eof(
        protocol="openai",
        policy="strict",
        snapshot=observer.completion_snapshot,
        downstream_started=True,
    )
    assert decision.classification == "premature_eof"
    assert decision.downstream_started


def test_markerless_usage_can_only_complete_under_provider_policy() -> None:
    observer = IncrementalSSEObserver("openai")
    observer.observe(
        b'data: {"choices": [], "usage": '
        b'{"prompt_tokens": 1, "completion_tokens": 1}}\n\n'
    )
    observer.flush()

    snapshot = observer.completion_snapshot
    assert (
        classify_stream_eof(
            protocol="openai",
            policy="strict",
            snapshot=snapshot,
            downstream_started=False,
        ).classification
        == "premature_eof"
    )
    assert (
        classify_stream_eof(
            protocol="openai",
            policy="compatible",
            snapshot=snapshot,
            downstream_started=False,
        ).classification
        == "compatibility_eof"
    )


# ---------------------------------------------------------------------------
# Plan 144 — Responses terminal event classifications
# ---------------------------------------------------------------------------


def test_responses_completed_classifies_as_complete() -> None:
    """``response.completed`` is the sole Responses success."""
    observer = IncrementalSSEObserver("openai", request_surface="responses")
    observer.observe(b"event: response.created\ndata: {}\n\n")
    observer.observe(b"event: response.output_text.delta\ndata: {}\n\n")
    observer.observe(b"event: response.completed\ndata: {}\n\n")
    observer.flush()

    decision = classify_stream_eof(
        protocol="openai",
        policy="strict",
        snapshot=observer.completion_snapshot,
        downstream_started=True,
    )
    assert decision.classification == "complete"


def test_responses_failed_classifies_as_terminal_failure() -> None:
    """``response.failed`` is a terminal non-success."""
    observer = IncrementalSSEObserver("openai", request_surface="responses")
    observer.observe(b"event: response.created\ndata: {}\n\n")
    observer.observe(b"event: response.failed\ndata: {}\n\n")
    observer.flush()

    snapshot = observer.completion_snapshot
    assert snapshot.terminal_kind == "responses_failed"
    decision = classify_stream_eof(
        protocol="openai",
        policy="strict",
        snapshot=snapshot,
        downstream_started=True,
    )
    assert decision.classification == "terminal_failure"


def test_responses_incomplete_classifies_as_terminal_incomplete() -> None:
    """``response.incomplete`` is a terminal non-success."""
    observer = IncrementalSSEObserver("openai", request_surface="responses")
    observer.observe(b"event: response.created\ndata: {}\n\n")
    observer.observe(b"event: response.incomplete\ndata: {}\n\n")
    observer.flush()

    snapshot = observer.completion_snapshot
    assert snapshot.terminal_kind == "responses_incomplete"
    decision = classify_stream_eof(
        protocol="openai",
        policy="strict",
        snapshot=snapshot,
        downstream_started=True,
    )
    assert decision.classification == "terminal_incomplete"


def test_responses_failed_no_terminal_is_premature() -> None:
    """Responses stream with deltas but no terminal event is premature."""
    observer = IncrementalSSEObserver("openai", request_surface="responses")
    observer.observe(b"event: response.created\ndata: {}\n\n")
    observer.observe(b"event: response.output_text.delta\ndata: {}\n\n")
    observer.flush()

    decision = classify_stream_eof(
        protocol="openai",
        policy="strict",
        snapshot=observer.completion_snapshot,
        downstream_started=True,
    )
    assert decision.classification == "premature_eof"
