"""Tests for the normalized usage helper.

Phase 1 of cache compression plan: provider-neutral usage observability.
This module verifies that ``NormalizedUsage`` is constructed correctly from
all upstream payload shapes (OpenAI, Anthropic, edge cases) and that the
cache counter status is reported faithfully so later phases can reason
about hit rates without ambiguity.
"""

from __future__ import annotations

import json
from dataclasses import replace

from eggpool.proxy.normalized_usage import (
    CacheCounterStatus,
    NormalizedUsage,
    UsageParseDiag,
    emit_parse_failure_log,
    normalize_from_stream_result,
    normalize_usage,
)

OPENAI_PROTOCOL = "openai"
ANTHROPIC_PROTOCOL = "anthropic"


def _payload(d: dict) -> dict:
    """Return the dict directly — normalize_usage expects a decoded payload."""
    return d


def _raw_body(d: dict) -> bytes:
    return json.dumps(d).encode()


# ---------------------------------------------------------------------------
# OpenAI path
# ---------------------------------------------------------------------------


def test_openai_reports_cache_status_when_present() -> None:
    body = _payload(
        {
            "id": "cmpl-1",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "prompt_tokens_details": {"cached_tokens": 80},
            },
        }
    )
    result = normalize_usage(body, protocol=OPENAI_PROTOCOL)
    assert result is not None
    assert result.cache_counter_status is CacheCounterStatus.REPORTED
    assert result.cached_input_tokens == 80
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.total_tokens == 150


def test_openai_marks_not_reported_when_cache_field_absent() -> None:
    body = _payload(
        {
            "id": "cmpl-2",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
            },
        }
    )
    result = normalize_usage(body, protocol=OPENAI_PROTOCOL)
    assert result is not None
    assert result.cache_counter_status is CacheCounterStatus.NOT_REPORTED
    assert result.cached_input_tokens is None
    assert result.input_tokens == 100


def test_openai_marks_not_reported_when_prompt_details_present_but_no_cache() -> None:
    body = _payload(
        {
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "prompt_tokens_details": {"audio_tokens": 1},
            },
        }
    )
    result = normalize_usage(body, protocol=OPENAI_PROTOCOL)
    assert result is not None
    assert result.cache_counter_status is CacheCounterStatus.NOT_REPORTED


def test_openai_unknown_shape_returns_unknown_status() -> None:
    """A non-dict payload yields UNKNOWN_FORMAT — caller is responsible
    for JSON decoding before invoking normalize_usage."""
    result = normalize_usage("not-json-at-all", protocol=OPENAI_PROTOCOL)  # type: ignore[arg-type]
    assert result is not None
    assert result.cache_counter_status is CacheCounterStatus.UNKNOWN_FORMAT
    assert result.input_tokens is None
    assert result.output_tokens is None


def test_openai_unknown_shape_returns_unknown_for_dict_without_usage() -> None:
    body = _payload({"id": "no-usage-here"})
    result = normalize_usage(body, protocol=OPENAI_PROTOCOL)
    assert result is not None
    assert result.cache_counter_status is CacheCounterStatus.UNKNOWN_FORMAT


def test_openai_zero_cached_tokens_reported() -> None:
    """Provider that explicitly returns cached_tokens=0 must be REPORTED."""
    body = _payload(
        {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "prompt_tokens_details": {"cached_tokens": 0},
            },
        }
    )
    result = normalize_usage(body, protocol=OPENAI_PROTOCOL)
    assert result is not None
    assert result.cache_counter_status is CacheCounterStatus.REPORTED
    assert result.cached_input_tokens == 0


# ---------------------------------------------------------------------------
# Anthropic path
# ---------------------------------------------------------------------------


def test_anthropic_reports_cache_read_and_creation() -> None:
    body = _payload(
        {
            "id": "msg_1",
            "usage": {
                "input_tokens": 200,
                "output_tokens": 75,
                "cache_read_input_tokens": 120,
                "cache_creation_input_tokens": 30,
            },
        }
    )
    result = normalize_usage(body, protocol=ANTHROPIC_PROTOCOL)
    assert result is not None
    assert result.cache_counter_status is CacheCounterStatus.REPORTED
    assert result.cached_input_tokens == 150
    assert result.cache_read_input_tokens == 120
    assert result.cache_creation_input_tokens == 30
    assert result.cache_write_input_tokens == 30
    assert result.input_tokens == 200
    assert result.output_tokens == 75


def test_anthropic_marks_not_reported_when_cache_keys_absent() -> None:
    body = _payload({"usage": {"input_tokens": 200, "output_tokens": 75}})
    result = normalize_usage(body, protocol=ANTHROPIC_PROTOCOL)
    assert result is not None
    assert result.cache_counter_status is CacheCounterStatus.NOT_REPORTED
    assert result.cached_input_tokens is None
    assert result.cache_creation_input_tokens is None


def test_anthropic_zero_cache_keys_still_reported() -> None:
    """Anthropic always emits the cache keys; explicit zeros are REPORTED."""
    body = _payload(
        {
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        }
    )
    result = normalize_usage(body, protocol=ANTHROPIC_PROTOCOL)
    assert result is not None
    assert result.cache_counter_status is CacheCounterStatus.REPORTED
    assert result.cached_input_tokens == 0


# ---------------------------------------------------------------------------
# Unknown protocol -> UNKNOWN_FORMAT when usage block is present
# ---------------------------------------------------------------------------


def test_unknown_protocol_with_usage_block_returns_unknown() -> None:
    body = _payload(
        {"usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}}
    )
    result = normalize_usage(body, protocol="weird-provider-protocol")
    assert result is not None
    assert result.cache_counter_status is CacheCounterStatus.UNKNOWN_FORMAT
    # The raw usage is preserved so operators can debug.
    assert result.raw_usage == {
        "input_tokens": 1,
        "output_tokens": 2,
        "total_tokens": 3,
    }


def test_none_protocol_with_usage_block_returns_unknown() -> None:
    body = _payload(
        {"usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}}
    )
    result = normalize_usage(body, protocol="")
    assert result is not None
    assert result.cache_counter_status is CacheCounterStatus.UNKNOWN_FORMAT


# ---------------------------------------------------------------------------
# Stream-result path
# ---------------------------------------------------------------------------


class _StubUsageResult:
    """Minimal stand-in for ``StreamUsageResult`` for unit tests."""

    def __init__(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_tokens = cache_read_tokens
        self.cache_creation_tokens = cache_creation_tokens


def test_stream_result_openai_cached_tokens_propagate() -> None:
    usage = _StubUsageResult(input_tokens=100, output_tokens=50, cache_read_tokens=80)
    result = normalize_from_stream_result(usage, protocol=OPENAI_PROTOCOL)
    assert result is not None
    assert result.cache_counter_status is CacheCounterStatus.REPORTED
    assert result.cached_input_tokens == 80
    assert result.input_tokens == 100


def test_stream_result_anthropic_cache_creation_propagates() -> None:
    usage = _StubUsageResult(
        input_tokens=200,
        output_tokens=75,
        cache_read_tokens=120,
        cache_creation_tokens=30,
    )
    result = normalize_from_stream_result(usage, protocol=ANTHROPIC_PROTOCOL)
    assert result is not None
    assert result.cache_counter_status is CacheCounterStatus.REPORTED
    assert result.cached_input_tokens == 150
    assert result.cache_read_input_tokens == 120
    assert result.cache_creation_input_tokens == 30
    assert result.cache_write_input_tokens == 30


def test_stream_result_none_input_marks_not_reported() -> None:
    """When the stream produced nothing, status is NOT_REPORTED."""
    result = normalize_from_stream_result(None, protocol=OPENAI_PROTOCOL)
    assert result.cache_counter_status is CacheCounterStatus.NOT_REPORTED
    assert result.input_tokens is None


def test_stream_result_zero_values_with_no_cache_keys_marks_not_reported() -> None:
    """All-zero usage with no cache fields observed => NOT_REPORTED."""
    usage = _StubUsageResult(input_tokens=100, output_tokens=50)
    result = normalize_from_stream_result(usage, protocol=OPENAI_PROTOCOL)
    assert result.cache_counter_status is CacheCounterStatus.NOT_REPORTED


# ---------------------------------------------------------------------------
# Diag logger smoke test
# ---------------------------------------------------------------------------


def test_emit_parse_failure_log_is_safe_to_call() -> None:
    """The logger must never raise even when the payload is malformed."""
    diag = UsageParseDiag(
        provider_id="openai",
        model_id="gpt-4",
        protocol=OPENAI_PROTOCOL,
        reason="not_json",
        raw_keys=[],
    )
    emit_parse_failure_log(diag)
    diag_unknown = UsageParseDiag(
        provider_id="anthropic",
        model_id="claude-3",
        protocol=ANTHROPIC_PROTOCOL,
        reason="unknown_shape",
        raw_keys=["x", "y"],
    )
    emit_parse_failure_log(diag_unknown)
    diag_missing = UsageParseDiag(
        provider_id=None,
        model_id=None,
        protocol="openai",
        reason="missing_final_stream_event",
    )
    emit_parse_failure_log(diag_missing)
    diag_preserved = UsageParseDiag(
        provider_id="p",
        model_id="m",
        protocol="openai",
        reason="something_else",
    )
    emit_parse_failure_log(diag_preserved)


# ---------------------------------------------------------------------------
# NormalizedUsage shape
# ---------------------------------------------------------------------------


def test_normalized_usage_is_frozen() -> None:
    usage = NormalizedUsage(
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        cached_input_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        cache_write_input_tokens=0,
        cache_counter_status=CacheCounterStatus.NOT_REPORTED,
        raw_usage=None,
    )
    # Frozen dataclass rejects in-place mutation but allows replace.
    new_usage = replace(usage, input_tokens=999)
    assert usage.input_tokens == 10
    assert new_usage.input_tokens == 999
    # hashable
    assert {usage}


def test_normalized_usage_accepts_none_token_values() -> None:
    usage = NormalizedUsage(cache_counter_status=CacheCounterStatus.NOT_REPORTED)
    assert usage.input_tokens is None
    assert usage.cached_input_tokens is None
    assert usage.raw_usage is None
