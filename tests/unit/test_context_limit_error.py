"""Tests for ContextLimitExceededError."""

from __future__ import annotations

from eggpool.errors import AggregatorError, ContextLimitExceededError


def test_is_aggregator_error() -> None:
    err = ContextLimitExceededError(
        model_id="m1",
        estimated_input_tokens=100000,
        requested_output_tokens=16384,
        max_context_tokens=220000,
        max_input_tokens=None,
    )
    assert isinstance(err, AggregatorError)


def test_properties() -> None:
    err = ContextLimitExceededError(
        model_id="MiniMax-M3/opencode-go",
        estimated_input_tokens=200000,
        requested_output_tokens=16384,
        max_context_tokens=220000,
        max_input_tokens=200000,
    )
    assert err.model_id == "MiniMax-M3/opencode-go"
    assert err.estimated_input_tokens == 200000
    assert err.requested_output_tokens == 16384
    assert err.max_context_tokens == 220000
    assert err.max_input_tokens == 200000


def test_message_contains_model() -> None:
    err = ContextLimitExceededError(
        model_id="m1",
        estimated_input_tokens=100000,
        requested_output_tokens=None,
        max_context_tokens=220000,
        max_input_tokens=None,
    )
    assert "m1" in str(err)


def test_optional_fields() -> None:
    err = ContextLimitExceededError(
        model_id="m1",
        estimated_input_tokens=100000,
        requested_output_tokens=None,
        max_context_tokens=None,
        max_input_tokens=None,
    )
    assert err.requested_output_tokens is None
    assert err.max_context_tokens is None
