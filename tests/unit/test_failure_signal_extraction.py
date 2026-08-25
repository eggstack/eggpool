"""Failure signal extraction tests.

Validates the bounded, conservative signal extractor against known
provider response patterns.
"""

from __future__ import annotations

import pytest

from eggpool.failure.signal import FailureSignal
from eggpool.failure.signal_extract import extract_failure_signal


class TestSignalExtraction:
    """Bounded signal extraction from response bodies."""

    def test_quota_exhausted_json(self) -> None:
        body = (
            b'{"error": {"message": "quota exhausted", '
            b'"type": "invalid_request_error"}}'
        )
        assert extract_failure_signal(body) == FailureSignal.QUOTA_EXHAUSTED

    def test_quota_exceeded_text(self) -> None:
        body = b"You have exceeded your quota limit."
        assert extract_failure_signal(body) == FailureSignal.QUOTA_EXHAUSTED

    def test_out_of_credits(self) -> None:
        body = b"Out of credits. Please add more."
        assert extract_failure_signal(body) == FailureSignal.QUOTA_EXHAUSTED

    def test_insufficient_balance(self) -> None:
        body = b"Insufficient balance for this request."
        assert extract_failure_signal(body) == FailureSignal.QUOTA_EXHAUSTED

    def test_rate_limit(self) -> None:
        body = b"Rate limited. Try again later."
        assert extract_failure_signal(body) == FailureSignal.RATE_LIMITED

    def test_too_many_requests(self) -> None:
        body = b"Too many requests. Slow down."
        assert extract_failure_signal(body) == FailureSignal.RATE_LIMITED

    def test_slow_down(self) -> None:
        body = b"Slow down. You are sending too many requests."
        assert extract_failure_signal(body) == FailureSignal.RATE_LIMITED

    def test_auth_failed(self) -> None:
        body = b"Authentication failed. Check your API key."
        assert extract_failure_signal(body) == FailureSignal.AUTHENTICATION_FAILED

    def test_unauthorized(self) -> None:
        body = b"Unauthorized access."
        assert extract_failure_signal(body) == FailureSignal.AUTHENTICATION_FAILED

    def test_invalid_api_key(self) -> None:
        body = b"Invalid API key provided."
        assert extract_failure_signal(body) == FailureSignal.AUTHENTICATION_FAILED

    def test_model_not_found(self) -> None:
        body = b"Model not found: gpt-4o-nonexistent"
        assert extract_failure_signal(body) == FailureSignal.MODEL_ABSENT

    def test_unknown_model(self) -> None:
        body = b"Unknown model: custom-model"
        assert extract_failure_signal(body) == FailureSignal.MODEL_ABSENT

    def test_unsupported_model(self) -> None:
        body = b"Unsupported model"
        assert extract_failure_signal(body) == FailureSignal.MODEL_ABSENT

    def test_no_such_model(self) -> None:
        body = b"No such model exists."
        assert extract_failure_signal(body) == FailureSignal.MODEL_ABSENT

    def test_context_limit_exceeded(self) -> None:
        body = b"context_limit_exceeded: maximum context length is 128k"
        assert extract_failure_signal(body) == FailureSignal.CONTEXT_LIMIT_EXCEEDED

    def test_token_limit_exceeded(self) -> None:
        body = b"Token limit exceeded for this model."
        assert extract_failure_signal(body) == FailureSignal.CONTEXT_LIMIT_EXCEEDED

    def test_unsupported_thinking_control(self) -> None:
        body = b"Unsupported thinking mode: extended"
        assert extract_failure_signal(body) == FailureSignal.UNSUPPORTED_REQUEST_CONTROL

    def test_thinking_not_supported(self) -> None:
        body = b"Thinking control not supported by this provider."
        assert extract_failure_signal(body) == FailureSignal.UNSUPPORTED_REQUEST_CONTROL

    def test_no_body_returns_none(self) -> None:
        assert extract_failure_signal(None) is None

    def test_empty_body_returns_none(self) -> None:
        assert extract_failure_signal(b"") is None

    def test_unknown_body_returns_none(self) -> None:
        body = b"Something went wrong but we don't know what."
        assert extract_failure_signal(body) is None

    def test_bounded_inspection(self) -> None:
        """Only first 4096 bytes are inspected."""
        prefix = b"quota exhausted. "
        suffix = b"x" * 10000
        assert extract_failure_signal(prefix + suffix) == FailureSignal.QUOTA_EXHAUSTED


class TestSignalFromErrorClass:
    """Fallback signal derivation from error class strings."""

    def test_context_limit_error_class(self) -> None:
        assert (
            extract_failure_signal(None, error_class="ContextLimitExceeded")
            == FailureSignal.CONTEXT_LIMIT_EXCEEDED
        )

    def test_quota_error_class(self) -> None:
        assert (
            extract_failure_signal(None, error_class="QuotaExhausted")
            == FailureSignal.QUOTA_EXHAUSTED
        )

    def test_rate_limit_error_class(self) -> None:
        assert (
            extract_failure_signal(None, error_class="RateLimitError")
            == FailureSignal.RATE_LIMITED
        )

    def test_model_unavailable_error_class(self) -> None:
        assert (
            extract_failure_signal(None, error_class="ModelUnavailable")
            == FailureSignal.MODEL_ABSENT
        )

    def test_auth_error_class(self) -> None:
        assert (
            extract_failure_signal(None, error_class="AuthenticationError")
            == FailureSignal.AUTHENTICATION_FAILED
        )

    @pytest.mark.parametrize(
        "error_class",
        [
            "authorization_pending",
            "authentication_timeout",
            "authoritative_timeout",
        ],
    )
    def test_auth_substring_classes_are_not_terminal(self, error_class: str) -> None:
        """Transient classes containing 'auth' must not map to terminal auth."""
        assert (
            extract_failure_signal(None, error_class=error_class)
            is not FailureSignal.AUTHENTICATION_FAILED
        )

    def test_capability_error_class(self) -> None:
        assert (
            extract_failure_signal(None, error_class="CapabilityError")
            == FailureSignal.UNSUPPORTED_REQUEST_CONTROL
        )

    def test_status_code_401(self) -> None:
        assert (
            extract_failure_signal(None, status_code=401)
            == FailureSignal.AUTHENTICATION_FAILED
        )

    def test_status_code_402(self) -> None:
        assert (
            extract_failure_signal(None, status_code=402)
            == FailureSignal.QUOTA_EXHAUSTED
        )

    def test_status_code_429(self) -> None:
        assert (
            extract_failure_signal(None, status_code=429) == FailureSignal.RATE_LIMITED
        )

    def test_no_match_returns_none(self) -> None:
        assert (
            extract_failure_signal(None, error_class="SomeOtherError", status_code=500)
            is None
        )
