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
            downstream_bytes_emitted=len(payload),
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


def test_payload_without_terminal_is_premature_and_not_retryable_after_bytes() -> None:
    observer = IncrementalSSEObserver("openai")
    observer.observe(b'data: {"choices": []}\n\n')
    observer.flush()

    decision = classify_stream_eof(
        protocol="openai",
        policy="strict",
        snapshot=observer.completion_snapshot,
        downstream_bytes_emitted=1,
    )
    assert decision.classification == "premature_eof"
    assert not decision.retryable


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
            downstream_bytes_emitted=0,
        ).classification
        == "premature_eof"
    )
    assert (
        classify_stream_eof(
            protocol="openai",
            policy="compatible",
            snapshot=snapshot,
            downstream_bytes_emitted=0,
        ).classification
        == "compatibility_eof"
    )
