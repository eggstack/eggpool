"""Retry classification for upstream failures."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RetryCategory(Enum):
    """Categories of retryable errors."""

    NEVER = "never"  # Never retry (e.g., 400, 401)
    BAD_REQUEST = "bad_request"  # Client error, don't retry
    AUTH_FAILURE = "auth_failure"  # Authentication failure
    QUOTA_EXCEEDED = "quota_exceeded"  # Rate limit or quota exceeded
    TEMPORARY = "temporary"  # Temporary error, retry with backoff
    TRANSIENT = "transient"  # Transient error, retry immediately
    FATAL = "fatal"  # Fatal error, don't retry


@dataclass
class RetryableError:
    """Represents a retryable error."""

    status_code: int
    category: RetryCategory
    retry_after: float | None = None
    message: str = ""
    account_name: str | None = None
    model_id: str | None = None

    @property
    def is_retryable(self) -> bool:
        """Check if this error is retryable."""
        return self.category in (
            RetryCategory.TEMPORARY,
            RetryCategory.TRANSIENT,
            RetryCategory.QUOTA_EXCEEDED,
        )

    @property
    def should_disable_account(self) -> bool:
        """Check if this error should disable the account."""
        return self.category == RetryCategory.AUTH_FAILURE

    @property
    def should_remove_model(self) -> bool:
        """Check if this error should remove the model from the account."""
        return self.status_code == 404


class RetryClassifier:
    """Classifies errors for retry decisions."""

    def classify(
        self, status_code: int, headers: dict[str, str] | None = None
    ) -> RetryableError:
        """Classify a status code into a retry category."""
        headers = headers or {}

        if status_code == 400:
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.BAD_REQUEST,
                message="Bad request - client error",
            )
        elif status_code == 401:
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.AUTH_FAILURE,
                message="Authentication failed",
            )
        elif status_code == 403:
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.AUTH_FAILURE,
                message="Forbidden",
            )
        elif status_code == 404:
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.BAD_REQUEST,
                message="Model not found",
            )
        elif status_code == 402:
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.QUOTA_EXCEEDED,
                message="Payment required - quota exceeded",
            )
        elif status_code == 429:
            retry_after = self._parse_retry_after(headers.get("retry-after"))
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.QUOTA_EXCEEDED,
                retry_after=retry_after,
                message="Rate limited",
            )
        elif status_code == 500:
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.TEMPORARY,
                message="Internal server error",
            )
        elif status_code == 502:
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.TRANSIENT,
                message="Bad gateway",
            )
        elif status_code == 503:
            retry_after = self._parse_retry_after(headers.get("retry-after"))
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.TEMPORARY,
                retry_after=retry_after,
                message="Service unavailable",
            )
        elif status_code == 504:
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.TRANSIENT,
                message="Gateway timeout",
            )
        else:
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.TEMPORARY,
                message=f"Unknown error: {status_code}",
            )

    def _parse_retry_after(self, value: str | None) -> float | None:
        """Parse Retry-After header value."""
        if value is None:
            return None
        try:
            return float(value)
        except ValueError:
            return None
