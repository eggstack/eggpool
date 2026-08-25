"""Bounded response signal extraction.

Extracts conservative, bounded signals from upstream failure responses.
Inspects a bounded response prefix and structured JSON fields, then
discards content.  Raw response bodies are never stored or propagated
in observations.
"""

from __future__ import annotations

import re

from eggpool.failure.signal import FailureSignal
from eggpool.health.health_manager import AUTH_FAILURE_ERROR_CLASSES

# Maximum bytes to inspect from response body
_MAX_SIGNAL_INSPECT_BYTES = 4096

# Quota-related signal patterns
_QUOTA_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bquota\s*(exhausted|exceeded|limit)\b", re.IGNORECASE),
    re.compile(r"\bout\s*of\s*(credits?|tokens?|quota)\b", re.IGNORECASE),
    re.compile(r"\binsufficient[_\s-]?(credits?|balance|quota)\b", re.IGNORECASE),
    re.compile(r"\baccount[_\s-]?(limit|suspended)\b", re.IGNORECASE),
)

# Rate-limit signal patterns
_RATE_LIMIT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brate[_\s-]?limit(?:ed)?\b", re.IGNORECASE),
    re.compile(r"\btoo\s*many\s*requests\b(?![_\s-]?in[_\s-]?queue)", re.IGNORECASE),
    re.compile(r"\bslow[_\s-]?down\b", re.IGNORECASE),
)

# Auth failure signal patterns
_AUTH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bunauthorized\b", re.IGNORECASE),
    re.compile(r"\bauthentication\s*(failed|error|invalid)\b", re.IGNORECASE),
    re.compile(r"\binvalid[_\s-]?(api[_\s-]?key|token|credential)\b", re.IGNORECASE),
)

# Model-specific signal patterns
_MODEL_ABSENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bmodel\s+not\s+found\b", re.IGNORECASE),
    re.compile(r"\bunknown\s+model\b", re.IGNORECASE),
    re.compile(r"\bunsupported\s+model\b", re.IGNORECASE),
    re.compile(r"\bmodel\s+is\s+not\s+available\b", re.IGNORECASE),
    re.compile(r"\bmodel\s+does\s+not\s+exist\b", re.IGNORECASE),
    re.compile(r"\bno\s+such\s+model\b", re.IGNORECASE),
    re.compile(r"\bmodel_id\s+not\s+found\b", re.IGNORECASE),
)

# Context limit signal patterns
_CONTEXT_LIMIT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bcontext[_\s-]?limit[_\s-]?exceeded\b", re.IGNORECASE),
    re.compile(r"\bcontext[_\s-]?length[_\s-]?(exceeded|too\s+long)\b", re.IGNORECASE),
    re.compile(r"\bmaximum\s+context\s+length\b", re.IGNORECASE),
    re.compile(r"\btoken\s+limit\s+exceeded\b", re.IGNORECASE),
)

# Unsupported control signal patterns
_CONTROL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bunsupported[_\s-]?(thinking|reasoning|control)\b", re.IGNORECASE),
    re.compile(
        r"\bthinking[_\s-]?(mode|control)\s+not[_\s-]?supported\b", re.IGNORECASE
    ),
)


def extract_failure_signal(
    body: bytes | None,
    *,
    error_class: str | None = None,
    status_code: int | None = None,
) -> FailureSignal | None:
    """Extract a conservative signal from a failure response body.

    Inspects at most ``_MAX_SIGNAL_INSPECT_BYTES`` from the response.
    Returns ``None`` when no signal can be extracted, letting the
    caller fall through to the default classification.
    """
    if body is None:
        return _signal_from_error_class(error_class, status_code)

    try:
        text = body[:_MAX_SIGNAL_INSPECT_BYTES].decode("utf-8", errors="replace")
    except Exception:
        return _signal_from_error_class(error_class, status_code)

    # Check quota signals first (highest priority)
    for pattern in _QUOTA_PATTERNS:
        if pattern.search(text):
            return FailureSignal.QUOTA_EXHAUSTED

    # Rate limit signals
    for pattern in _RATE_LIMIT_PATTERNS:
        if pattern.search(text):
            return FailureSignal.RATE_LIMITED

    # Auth failure signals
    for pattern in _AUTH_PATTERNS:
        if pattern.search(text):
            return FailureSignal.AUTHENTICATION_FAILED

    # Model-specific signals
    for pattern in _MODEL_ABSENT_PATTERNS:
        if pattern.search(text):
            return FailureSignal.MODEL_ABSENT

    # Context limit signals
    for pattern in _CONTEXT_LIMIT_PATTERNS:
        if pattern.search(text):
            return FailureSignal.CONTEXT_LIMIT_EXCEEDED

    # Unsupported control signals
    for pattern in _CONTROL_PATTERNS:
        if pattern.search(text):
            return FailureSignal.UNSUPPORTED_REQUEST_CONTROL

    return _signal_from_error_class(error_class, status_code)


def _signal_from_error_class(
    error_class: str | None,
    status_code: int | None,
) -> FailureSignal | None:
    """Derive a signal from the error class string and status code.

    Used as a fallback when no body is available or the body did not
    match any known pattern.
    """
    if error_class is not None:
        ec = error_class.lower()
        if "contextlimitexceeded" in ec or "context_limit_exceeded" in ec:
            return FailureSignal.CONTEXT_LIMIT_EXCEEDED
        if "quotaexhausted" in ec or "quota_exhausted" in ec:
            return FailureSignal.QUOTA_EXHAUSTED
        if "ratelimit" in ec or "rate_limit" in ec:
            return FailureSignal.RATE_LIMITED
        if "modelunavailable" in ec or "model_not_found" in ec:
            return FailureSignal.MODEL_ABSENT
        # Exact-match vocabulary shared with
        # ``classify_failure_category`` — substring matching would map
        # transient classes like ``authorization_pending`` onto a
        # terminal auth signal.
        if ec in AUTH_FAILURE_ERROR_CLASSES:
            return FailureSignal.AUTHENTICATION_FAILED
        if "capability" in ec or "unsupported" in ec:
            return FailureSignal.UNSUPPORTED_REQUEST_CONTROL
    # Status code hints (only for well-known codes)
    if status_code == 401:
        return FailureSignal.AUTHENTICATION_FAILED
    if status_code == 402:
        return FailureSignal.QUOTA_EXHAUSTED
    if status_code == 429:
        return FailureSignal.RATE_LIMITED
    return None
