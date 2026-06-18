"""Unit tests for retry classification (Phase 14)."""

from __future__ import annotations

import pytest

from go_aggregator.retry.classification import RetryCategory, RetryClassifier


@pytest.fixture()
def classifier() -> RetryClassifier:
    return RetryClassifier()


class TestExplicitStatuses:
    """Statuses with explicit handling remain unchanged."""

    def test_400_bad_request(self, classifier: RetryClassifier) -> None:
        result = classifier.classify(400)
        assert result.category == RetryCategory.BAD_REQUEST
        assert not result.is_retryable

    def test_401_auth_failure(self, classifier: RetryClassifier) -> None:
        result = classifier.classify(401)
        assert result.category == RetryCategory.AUTH_FAILURE
        assert result.should_disable_account

    def test_402_quota_exceeded(self, classifier: RetryClassifier) -> None:
        result = classifier.classify(402)
        assert result.category == RetryCategory.QUOTA_EXCEEDED
        assert result.is_retryable

    def test_403_auth_failure(self, classifier: RetryClassifier) -> None:
        result = classifier.classify(403)
        assert result.category == RetryCategory.AUTH_FAILURE

    def test_404_not_model_specific(self, classifier: RetryClassifier) -> None:
        result = classifier.classify(404, body=b'{"error": "not found"}')
        assert result.category == RetryCategory.BAD_REQUEST
        assert not result.is_retryable

    def test_404_model_specific(self, classifier: RetryClassifier) -> None:
        result = classifier.classify(404, body=b'{"error": "model not found"}')
        assert result.category == RetryCategory.MODEL_UNAVAILABLE
        assert result.is_retryable

    def test_429_quota_exceeded(self, classifier: RetryClassifier) -> None:
        result = classifier.classify(429, headers={"retry-after": "5"})
        assert result.category == RetryCategory.QUOTA_EXCEEDED
        assert result.is_retryable
        assert result.retry_after == 5.0

    def test_500_temporary(self, classifier: RetryClassifier) -> None:
        result = classifier.classify(500)
        assert result.category == RetryCategory.TEMPORARY
        assert result.is_retryable

    def test_502_transient(self, classifier: RetryClassifier) -> None:
        result = classifier.classify(502)
        assert result.category == RetryCategory.TRANSIENT
        assert result.is_retryable

    def test_503_temporary(self, classifier: RetryClassifier) -> None:
        result = classifier.classify(503)
        assert result.category == RetryCategory.TEMPORARY
        assert result.is_retryable

    def test_504_transient(self, classifier: RetryClassifier) -> None:
        result = classifier.classify(504)
        assert result.category == RetryCategory.TRANSIENT
        assert result.is_retryable


class TestArbitraryClientErrors:
    """Phase 14: Arbitrary 4xx errors must NOT be retried."""

    @pytest.mark.parametrize("status", [405, 409, 413, 415, 422])
    def test_unhandled_4xx_not_retryable(
        self, classifier: RetryClassifier, status: int
    ) -> None:
        result = classifier.classify(status)
        assert result.category == RetryCategory.BAD_REQUEST
        assert not result.is_retryable


class TestArbitraryServerErrors:
    """Phase 14: Unhandled 5xx errors ARE retryable."""

    @pytest.mark.parametrize("status", [501, 505, 507, 511])
    def test_unhandled_5xx_retryable(
        self, classifier: RetryClassifier, status: int
    ) -> None:
        result = classifier.classify(status)
        assert result.category == RetryCategory.TEMPORARY
        assert result.is_retryable


class TestOtherStatuses:
    """Non-4xx/5xx statuses are never retried."""

    def test_200_never(self, classifier: RetryClassifier) -> None:
        result = classifier.classify(200)
        assert result.category == RetryCategory.NEVER
        assert not result.is_retryable

    def test_301_never(self, classifier: RetryClassifier) -> None:
        result = classifier.classify(301)
        assert result.category == RetryCategory.NEVER
        assert not result.is_retryable


class TestRetryAfterParsing:
    """Retry-After header parsing for numeric and HTTP-date values."""

    def test_numeric_seconds(self, classifier: RetryClassifier) -> None:
        result = classifier.classify(429, headers={"retry-after": "30"})
        assert result.retry_after == 30.0

    def test_numeric_zero(self, classifier: RetryClassifier) -> None:
        result = classifier.classify(429, headers={"retry-after": "0"})
        assert result.retry_after == 0.0

    def test_http_date_future(self, classifier: RetryClassifier) -> None:
        import time

        future_ts = time.time() + 120
        import email.utils

        date_str = email.utils.formatdate(future_ts, usegmt=True)
        result = classifier.classify(429, headers={"retry-after": date_str})
        assert result.retry_after is not None
        assert 119.0 < result.retry_after < 121.0

    def test_http_date_past(self, classifier: RetryClassifier) -> None:
        import email.utils
        import time

        past_ts = time.time() - 60
        date_str = email.utils.formatdate(past_ts, usegmt=True)
        result = classifier.classify(429, headers={"retry-after": date_str})
        assert result.retry_after is not None
        assert result.retry_after == 0.0

    def test_invalid_value_returns_none(self, classifier: RetryClassifier) -> None:
        result = classifier.classify(429, headers={"retry-after": "not-a-date"})
        assert result.retry_after is None

    def test_none_value_returns_none(self, classifier: RetryClassifier) -> None:
        result = classifier.classify(429)
        assert result.retry_after is None

    def test_503_with_http_date(self, classifier: RetryClassifier) -> None:
        import email.utils
        import time

        future_ts = time.time() + 60
        date_str = email.utils.formatdate(future_ts, usegmt=True)
        result = classifier.classify(503, headers={"retry-after": date_str})
        assert result.retry_after is not None
        assert 59.0 < result.retry_after < 61.0
