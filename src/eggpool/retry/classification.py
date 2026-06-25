"""Retry classification for upstream failures."""

from __future__ import annotations

import datetime as dt
import email.utils
import math
import re
import time
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
        headers = {name.lower(): value for name, value in (headers or {}).items()}

        # Phase 6: when the status code is ambiguous (e.g. a generic
        # 403 or 409 that some providers use to signal quota
        # exhaustion), inspect the body for known quota/rate-limit
        # signals before falling through to the generic branch.
        body_signal = self._extract_provider_signal(body)

        if status_code == 400:
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.BAD_REQUEST,
                message="Bad request - client error",
            )
        if status_code == 401:
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.AUTH_FAILURE,
                message="Authentication failed",
            )
        if status_code == 403:
            if body_signal is RetryCategory.QUOTA_EXCEEDED:
                return RetryableError(
                    status_code=status_code,
                    category=RetryCategory.QUOTA_EXCEEDED,
                    message="Forbidden: provider signalled quota exhaustion",
                )
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.AUTH_FAILURE,
                message="Forbidden",
            )
        if status_code == 404:
            # Check if this is a model-specific 404 (retryable on another account)
            if body is not None and self._is_model_specific_404(body):
                return RetryableError(
                    status_code=status_code,
                    category=RetryCategory.MODEL_UNAVAILABLE,
                    message="Model unavailable on this account",
                )
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.BAD_REQUEST,
                message="Not found",
            )
        if status_code == 402:
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.QUOTA_EXCEEDED,
                message="Payment required - quota exceeded",
            )
        if status_code == 408:
            # Request timeout. Treated as transient so the next
            # account gets a chance; not a client error.
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.TRANSIENT,
                message="Request timeout",
            )
        if status_code in (409, 422):
            # Provider-specific. Honor explicit quota/rate-limit
            # signals in the body when present; otherwise treat as a
            # bad request so we do not silently suppress the account.
            if body_signal is RetryCategory.QUOTA_EXCEEDED:
                return RetryableError(
                    status_code=status_code,
                    category=RetryCategory.QUOTA_EXCEEDED,
                    message=(
                        f"Provider-specific {status_code} signalled quota/rate-limit"
                    ),
                )
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.BAD_REQUEST,
                message=f"Provider-specific client error: {status_code}",
            )
        if status_code == 429:
            retry_after = self._parse_retry_after(headers.get("retry-after"))
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.QUOTA_EXCEEDED,
                retry_after=retry_after,
                message="Rate limited",
            )
        if status_code == 500:
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.TEMPORARY,
                message="Internal server error",
            )
        if status_code == 502:
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.TRANSIENT,
                message="Bad gateway",
            )
        if status_code == 503:
            retry_after = self._parse_retry_after(headers.get("retry-after"))
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.TEMPORARY,
                retry_after=retry_after,
                message="Service unavailable",
            )
        if status_code == 504:
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.TRANSIENT,
                message="Gateway timeout",
            )
        if 400 <= status_code < 500:
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.BAD_REQUEST,
                message=f"Non-retryable client error: {status_code}",
            )
        if 500 <= status_code < 600:
            return RetryableError(
                status_code=status_code,
                category=RetryCategory.TEMPORARY,
                message=f"Temporary upstream error: {status_code}",
            )
        return RetryableError(
            status_code=status_code,
            category=RetryCategory.NEVER,
            message=f"Unclassified upstream status: {status_code}",
        )

    def _is_model_specific_404(self, body: bytes) -> bool:
        """Check if a 404 response body indicates a model-specific error."""
        try:
            text = body.decode("utf-8", errors="replace").lower()
        except Exception:
            return False
        model_signals = [
            "model not found",
            "unknown model",
            "unsupported model",
            "model is not available",
            "model does not exist",
            "no such model",
            "model_id not found",
        ]
        return any(signal in text for signal in model_signals)

    # Phase 6: provider-body signal detection. The patterns below are
    # intentionally conservative; ambiguous matches are demoted to
    # the generic client error path so we do not silently suppress an
    # account based on incidental body wording.
    _QUOTA_SIGNAL_PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(r"\bquota\s*(exhausted|exceeded|limit)\b", re.IGNORECASE),
        re.compile(r"\bout\s*of\s*(credits?|tokens?|quota)\b", re.IGNORECASE),
        re.compile(r"\binsufficient[_\s-]?(credits?|balance|quota)\b", re.IGNORECASE),
        re.compile(r"\baccount[_\s-]?(limit|suspended)\b", re.IGNORECASE),
    )
    _RATE_LIMIT_SIGNAL_PATTERNS: tuple[re.Pattern[str], ...] = (
        # Bare "rate limit" / "too many requests" are too easy to
        # match accidentally. Require a copula so we do not flag
        # queue-position messages like "too many requests in queue".
        re.compile(r"\brate[_\s-]?limit(?:ed)?\b", re.IGNORECASE),
        re.compile(
            r"\btoo\s*many\s*requests\b(?![_\s-]?in[_\s-]?queue)", re.IGNORECASE
        ),
        re.compile(r"\bslow[_\s-]?down\b", re.IGNORECASE),
    )

    def _extract_provider_signal(self, body: bytes | None) -> RetryCategory | None:
        """Return a retry category hinted by the provider response body.

        Used as a tiebreaker for ambiguous status codes (403, 409,
        422). Returns ``None`` when the body does not match any known
        signal so the caller can fall through to the generic branch.
        """
        if not body:
            return None
        try:
            text = body.decode("utf-8", errors="replace")
        except Exception:
            return None
        # Quota signals take precedence over rate-limit signals because
        # "out of credits" usually implies a longer suppression than
        # a transient 429-style message.
        for pattern in self._QUOTA_SIGNAL_PATTERNS:
            if pattern.search(text):
                return RetryCategory.QUOTA_EXCEEDED
        for pattern in self._RATE_LIMIT_SIGNAL_PATTERNS:
            if pattern.search(text):
                return RetryCategory.QUOTA_EXCEEDED
        return None

    def parse_retry_after(
        self,
        headers: dict[str, str] | None,
        default: float = 60.0,
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
