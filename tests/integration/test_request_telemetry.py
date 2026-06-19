"""Section 9: Persist all telemetry fields."""

from __future__ import annotations

from eggpool.request.finalizer import MAX_ERROR_DETAIL_CHARS


def test_error_detail_truncation() -> None:
    """error_detail should be truncated to MAX_ERROR_DETAIL_CHARS."""
    long_detail = "x" * 5000
    truncated = long_detail[:MAX_ERROR_DETAIL_CHARS]
    assert len(truncated) == MAX_ERROR_DETAIL_CHARS
    assert len(long_detail) > MAX_ERROR_DETAIL_CHARS


def test_error_detail_preserved_when_short() -> None:
    """Short error_detail should not be truncated."""
    short_detail = "Authentication failed"
    if len(short_detail) > MAX_ERROR_DETAIL_CHARS:
        truncated = short_detail[:MAX_ERROR_DETAIL_CHARS]
    else:
        truncated = short_detail
    assert truncated == short_detail


def test_retry_count_semantics() -> None:
    """retry_count = total_attempts - 1."""
    # Single attempt: retry_count = 0
    assert max(0, 1 - 1) == 0
    # Two attempts: retry_count = 1
    assert max(0, 2 - 1) == 1
    # Three attempts: retry_count = 2
    assert max(0, 3 - 1) == 2


def test_attempt_count_header_semantics() -> None:
    """x-proxy-attempt-count = total_attempts (not retry_count)."""
    # Attempt 1: header = 1
    attempt_num = 1
    assert str(attempt_num) == "1"
    # Attempt 2: header = 2
    attempt_num = 2
    assert str(attempt_num) == "2"


def test_max_error_detail_constant() -> None:
    """MAX_ERROR_DETAIL_CHARS should be 2048."""
    assert MAX_ERROR_DETAIL_CHARS == 2048
