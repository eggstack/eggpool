"""Retry classification for upstream failures."""

from __future__ import annotations

import datetime as dt
import email.utils
import math
import time
from dataclasses import dataclass
from enum import Enum

from eggpool.failure.classifier import classify_failure_effects
from eggpool.failure.observation import FailureObservation
from eggpool.failure.signal import FailureSignal
from eggpool.failure.signal_extract import extract_failure_signal


class RetryCategory(Enum):
    """Categories of retryable errors."""

    NEVER = "never"  # Never retry (e.g., 400, 401)
    BAD_REQUEST = "bad_request"  # Client error, don't retry
    AUTH_FAILURE = "auth_failure"  # Authentication failure
    QUOTA_EXCEEDED = "quota_exceeded"  # Rate limit or quota exceeded
    TEMPORARY = "temporary"  # Temporary error, retry with backoff
    TRANSIENT = "transient"  # Transient error, retry immediately
    FATAL = "fatal"  # Fatal error, don't retry
    MODEL_UNAVAILABLE = (
        "model_unavailable"  # Model-specific 404, retryable on another account
    )


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
            RetryCategory.AUTH_FAILURE,
            RetryCategory.TEMPORARY,
            RetryCategory.TRANSIENT,
            RetryCategory.QUOTA_EXCEEDED,
            RetryCategory.MODEL_UNAVAILABLE,
        )

    @property
    def should_disable_account(self) -> bool:
        """Check if this error should disable the account."""
        return self.category == RetryCategory.AUTH_FAILURE

    @property
    def should_disable_model(self) -> bool:
        """Check if this error should disable the model from this account."""
        return self.category == RetryCategory.MODEL_UNAVAILABLE

    @property
    def should_remove_model(self) -> bool:
        """Check if this error should remove the model from the account."""
        return (
            self.status_code == 404 and self.category == RetryCategory.MODEL_UNAVAILABLE
        )


class RetryClassifier:
    """Classifies errors for retry decisions."""

    def classify(
        self,
        status_code: int,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> RetryableError:
        """Classify a status code into a retry category."""
        normalized_headers = {
            name.lower(): value for name, value in (headers or {}).items()
        }
        retry_after = self._parse_retry_after(normalized_headers.get("retry-after"))
        signal = extract_failure_signal(body, status_code=status_code)
        observation = FailureObservation(
            source="upstream_http",
            status_code=status_code,
            error_class=None,
            provider_id=None,
            account_name=None,
            model_id=None,
            upstream_model_id=None,
            client_protocol="openai",
            upstream_protocol="openai",
            response_signal=signal,
            retry_after_s=retry_after,
            response_started=False,
        )
        effects = classify_failure_effects(observation)
        category = self._category_for(status_code, effects, signal)
        return RetryableError(
            status_code=status_code,
            category=category,
            retry_after=retry_after,
            message=effects.evidence_class,
        )

    @staticmethod
    def _category_for(
        status_code: int,
        effects: object,
        signal: FailureSignal | None,
    ) -> RetryCategory:
        """Adapt the canonical decision to the historical retry enum."""
        decision = effects
        if getattr(decision, "account_effect", "none") == "disable_auth":
            return RetryCategory.AUTH_FAILURE
        if getattr(decision, "model_effect", "none") != "none":
            return RetryCategory.MODEL_UNAVAILABLE
        if getattr(decision, "account_effect", "none") in {"quota", "rate_limit"}:
            return RetryCategory.QUOTA_EXCEEDED
        if getattr(decision, "retry", False):
            if status_code in {408, 502, 504}:
                return RetryCategory.TRANSIENT
            return RetryCategory.TEMPORARY
        if 400 <= status_code < 500:
            return RetryCategory.BAD_REQUEST
        if signal is not None and signal == FailureSignal.TRANSPORT_FAILURE:
            return RetryCategory.TEMPORARY
        return RetryCategory.NEVER

    def _is_model_specific_404(self, body: bytes) -> bool:
        """Check if a 404 response body indicates a model-specific error."""
        return extract_failure_signal(body) == FailureSignal.MODEL_ABSENT

    def _extract_provider_signal(self, body: bytes | None) -> RetryCategory | None:
        """Compatibility adapter for the former body-signal helper."""
        signal = extract_failure_signal(body)
        if signal in {FailureSignal.QUOTA_EXHAUSTED, FailureSignal.RATE_LIMITED}:
            return RetryCategory.QUOTA_EXCEEDED
        return None

    def parse_retry_after(
        self,
        headers: dict[str, str] | None,
        default: float | None = 60.0,
    ) -> float | None:
        """Public wrapper around ``_parse_retry_after`` for reuse.

        Accepts either a headers dict (case-insensitive) or ``None``.
        Returns the parsed ``Retry-After`` value or ``default`` when
        the header is missing/unparseable. Returns ``None`` only when
        ``default`` is explicitly set to ``None`` AND no header is
        present (the caller wants ``None`` to mean "no Retry-After
        was given").

        Parameters
        ----------
        headers:
            Mapping of HTTP header names to values. ``None`` is
            treated as empty.
        default:
            Fallback value when the header is missing or invalid.
            Pass ``None`` to distinguish "no header" from a numeric
            fallback.
        """
        if not headers:
            return default
        normalized = {name.lower(): value for name, value in headers.items()}
        value = normalized.get("retry-after")
        if value is None:
            return default
        parsed = self._parse_retry_after(value)
        if parsed is None:
            return default
        return parsed

    def _parse_retry_after(self, value: str | None) -> float | None:
        """Parse Retry-After header value.

        Supports both numeric seconds and HTTP-date formats per RFC 7231.
        """
        if value is None:
            return None
        # Try numeric seconds first
        try:
            seconds = float(value)
            return max(0.0, seconds) if math.isfinite(seconds) else None
        except ValueError:
            pass
        # Try HTTP-date (e.g. "Wed, 18 Jun 2026 21:00:00 GMT")
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.UTC)
            seconds = parsed.timestamp() - time.time()
            return max(0.0, seconds) if math.isfinite(seconds) else None
        except (OverflowError, OSError, TypeError, ValueError):
            return None
