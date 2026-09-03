"""Bounded and conservative extraction of upstream failure signals."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from eggpool.failure.signal import FailureSignal

if TYPE_CHECKING:
    from eggpool.failure.observation import ProviderModelPresence
from eggpool.health.health_manager import AUTH_FAILURE_ERROR_CLASSES
from eggpool.jsonx import loads as jsonx_loads

_MAX_SIGNAL_INSPECT_BYTES = 4096
_QUOTA_PATTERNS = (
    re.compile(r"\bquota\s*(exhausted|exceeded|limit)\b", re.I),
    re.compile(r"\bout\s*of\s*(credits?|tokens?|quota)\b", re.I),
    re.compile(r"\binsufficient[_\s-]?(credits?|balance|quota)\b", re.I),
    re.compile(r"\baccount[_\s-]?(limit|suspended)\b", re.I),
)
_RATE_LIMIT_PATTERNS = (
    re.compile(r"\brate[_\s-]?limit(?:ed)?\b", re.I),
    re.compile(r"\btoo\s*many\s*requests\b(?![_\s-]?in[_\s-]?queue)", re.I),
    re.compile(r"\bslow[_\s-]?down\b", re.I),
)
# Generic unauthorized/authentication text is intentionally excluded: it can
# describe a wrong endpoint/header contract rather than a bad credential.
_CREDENTIAL_INVALID_PATTERNS = (
    re.compile(r"\binvalid[_\s-]?(api[_\s-]?key|token|credential)\b", re.I),
    re.compile(r"\b(expired|revoked)\s+(api[_\s-]?key|token|credential)\b", re.I),
    re.compile(
        r"\b(api[_\s-]?key|token|credential)\s+(is\s+)?(expired|revoked)\b", re.I
    ),
)
_WIRE_AUTH_PATTERNS = (
    re.compile(r"\bmissing\s+(api[_\s-]?key|token|credential|authentication)\b", re.I),
    re.compile(
        r"\b(api[_\s-]?key|authorization|authentication)\s+header\s+(is\s+)?required\b",
        re.I,
    ),
    re.compile(r"\bx-api-key\s+required\b", re.I),
)
_STRONG_MODEL_ABSENT_PATTERNS = (
    re.compile(r"\bmodel(?:\s+[\w./:-]+)?\s+not\s+found\b", re.I),
    re.compile(r"\bunknown\s+model\b", re.I),
    re.compile(
        r"\bmodel(?:\s+[\w./:-]+)?\s+does\s+not\s+exist\b",
        re.I,
    ),
    re.compile(r"\bno\s+such\s+model\b", re.I),
    re.compile(r"\bmodel_id\s+not\s+found\b", re.I),
)
_WEAK_MODEL_UNSUPPORTED_PATTERNS = (
    re.compile(r"\bmodel\b.*\bis\s+not\s+supported\b", re.I),
    re.compile(r"\bunsupported\s+model\b", re.I),
    re.compile(
        r"\bmodel(?:\s+[\w./:-]+)?\s+is\s+not\s+available\b",
        re.I,
    ),
)
_CONTEXT_LIMIT_PATTERNS = (
    re.compile(r"\bcontext[_\s-]?limit[_\s-]?exceeded\b", re.I),
    re.compile(r"\bcontext[_\s-]?length[_\s-]?(exceeded|too\s+long)\b", re.I),
    re.compile(r"\bmaximum\s+context\s+length\b", re.I),
    re.compile(r"\btoken\s+limit\s+exceeded\b", re.I),
)
_CONTROL_PATTERNS = (
    re.compile(r"\bunsupported[_\s-]?(thinking|reasoning|control)\b", re.I),
    re.compile(r"\bthinking[_\s-]?(mode|control)\s+not[_\s-]?supported\b", re.I),
    re.compile(
        r"\b(unknown|unsupported|unrecognized)\s+(parameter|field)\b.*"
        r"\b(thinking|reasoning|response[_\s-]?format)\b",
        re.I,
    ),
)
_WIRE_SCHEMA_PATTERNS = (
    re.compile(
        r"\b(expected|expects)\s+(a\s+)?(different\s+)?(request|payload|body|schema)\b",
        re.I,
    ),
    re.compile(
        r"\b(endpoint|api|surface)\s+(expects|requires)\s+.*\b(request|payload|body|schema)\b",
        re.I,
    ),
    re.compile(
        r"\b(invalid|unsupported)\s+(request\s+)?(shape|schema)\s+"
        r"(for\s+this\s+endpoint|on\s+this\s+surface)\b",
        re.I,
    ),
    re.compile(
        r"\b(request|payload|body)\s+(shape|schema)\s+(mismatch|does\s+not\s+match)\b",
        re.I,
    ),
    re.compile(
        r"\b(chat\s+completions|responses|messages)\s+(endpoint|api)\b.*\b(expected|requires|only)\b",
        re.I,
    ),
)
_UNSUPPORTED_SURFACE_PATTERNS = (
    re.compile(r"\b(unsupported|unknown|unavailable)\s+(api|endpoint|surface)\b", re.I),
    re.compile(r"\bmethod\s+not\s+allowed\b", re.I),
)


def extract_failure_signal(
    body: bytes | None,
    *,
    error_class: str | None = None,
    status_code: int | None = None,
    credential_configured: bool = False,
    alternate_wire_available: bool = False,
    provider_model_presence: ProviderModelPresence = "unknown",
    dispatch_phase: str = "response_status",
) -> FailureSignal | None:
    """Extract one bounded signal, never retaining raw response content."""
    if body is None:
        return _signal_from_error_class(
            error_class,
            status_code,
            alternate_wire_available=alternate_wire_available,
            provider_model_presence=provider_model_presence,
            dispatch_phase=dispatch_phase,
        )
    try:
        text = body[:_MAX_SIGNAL_INSPECT_BYTES].decode("utf-8", errors="replace")
    except Exception:
        return _signal_from_error_class(
            error_class,
            status_code,
            alternate_wire_available=alternate_wire_available,
            provider_model_presence=provider_model_presence,
            dispatch_phase=dispatch_phase,
        )
    evidence = " ".join((*_structured_error_values(text), text))

    # Strong model withdrawal remains authoritative.  Unsupported and
    # ambiguous availability wording is only surface-local when the selected
    # provider advertises the model and the response arrived before downstream
    # handoff.
    if _matches(_STRONG_MODEL_ABSENT_PATTERNS, evidence):
        return FailureSignal.MODEL_ABSENT
    if (
        provider_model_presence == "known"
        and alternate_wire_available
        and dispatch_phase == "response_status"
        and _matches(_WEAK_MODEL_UNSUPPORTED_PATTERNS, evidence)
    ):
        return FailureSignal.MODEL_UNSUPPORTED_ON_SURFACE
    if _matches(_WEAK_MODEL_UNSUPPORTED_PATTERNS, evidence):
        return FailureSignal.MODEL_ABSENT
    if _matches(_QUOTA_PATTERNS, evidence):
        return FailureSignal.QUOTA_EXHAUSTED
    if _matches(_RATE_LIMIT_PATTERNS, evidence):
        return FailureSignal.RATE_LIMITED
    if _matches(_CREDENTIAL_INVALID_PATTERNS, evidence):
        return FailureSignal.CREDENTIAL_INVALID
    if (
        credential_configured
        and alternate_wire_available
        and _matches(_WIRE_AUTH_PATTERNS, evidence)
    ):
        return FailureSignal.WIRE_AUTH_MISMATCH
    if _matches(_CONTEXT_LIMIT_PATTERNS, evidence):
        return FailureSignal.CONTEXT_LIMIT_EXCEEDED
    if _matches(_CONTROL_PATTERNS, evidence):
        return FailureSignal.UNSUPPORTED_REQUEST_CONTROL
    if _matches(_WIRE_SCHEMA_PATTERNS, evidence):
        return FailureSignal.WIRE_SCHEMA_MISMATCH
    if status_code in {404, 405} and alternate_wire_available:
        return FailureSignal.WIRE_SURFACE_UNSUPPORTED
    if alternate_wire_available and _matches(_UNSUPPORTED_SURFACE_PATTERNS, evidence):
        return FailureSignal.WIRE_SURFACE_UNSUPPORTED
    if status_code in {400, 422}:
        return FailureSignal.GENERIC_CLIENT_VALIDATION
    return _signal_from_error_class(
        error_class,
        status_code,
        alternate_wire_available=alternate_wire_available,
        provider_model_presence=provider_model_presence,
        dispatch_phase=dispatch_phase,
    )


def _matches(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def _signal_from_error_class(
    error_class: str | None,
    status_code: int | None,
    *,
    alternate_wire_available: bool = False,
    provider_model_presence: ProviderModelPresence = "unknown",
    dispatch_phase: str = "response_status",
) -> FailureSignal | None:
    """Use exact known classes and safe status hints only."""
    if error_class is not None:
        ec = error_class.lower()
        if "wire_auth_mismatch" in ec:
            return FailureSignal.WIRE_AUTH_MISMATCH
        if "wire_surface_unsupported" in ec:
            return FailureSignal.WIRE_SURFACE_UNSUPPORTED
        if "wire_schema_mismatch" in ec:
            return FailureSignal.WIRE_SCHEMA_MISMATCH
        if (
            provider_model_presence == "known"
            and alternate_wire_available
            and dispatch_phase == "response_status"
            and ("unsupported model" in ec or "model_not_supported" in ec)
        ):
            return FailureSignal.MODEL_UNSUPPORTED_ON_SURFACE
        if "contextlimitexceeded" in ec or "context_limit_exceeded" in ec:
            return FailureSignal.CONTEXT_LIMIT_EXCEEDED
        if "quotaexhausted" in ec or "quota_exhausted" in ec:
            return FailureSignal.QUOTA_EXHAUSTED
        if "ratelimit" in ec or "rate_limit" in ec:
            return FailureSignal.RATE_LIMITED
        if "modelunavailable" in ec or "model_not_found" in ec:
            return FailureSignal.MODEL_ABSENT
        if ec in AUTH_FAILURE_ERROR_CLASSES:
            return FailureSignal.CREDENTIAL_INVALID
        if "capability" in ec or "unsupported" in ec:
            return FailureSignal.UNSUPPORTED_REQUEST_CONTROL
    # A bare 401 is deliberately not evidence of invalid credentials.
    if status_code == 402:
        return FailureSignal.QUOTA_EXHAUSTED
    if status_code == 429:
        return FailureSignal.RATE_LIMITED
    if status_code in {404, 405} and alternate_wire_available:
        return FailureSignal.WIRE_SURFACE_UNSUPPORTED
    return None


def _structured_error_values(text: str) -> tuple[str, ...]:
    """Extract bounded structured error fields without retaining the body."""
    try:
        payload: Any = jsonx_loads(text)
    except (TypeError, ValueError):
        return ()
    values: list[str] = []

    def visit(value: object, *, key: str | None = None) -> None:
        if isinstance(value, Mapping):
            mapping = cast("Mapping[object, object]", value)
            for child_key, child_value in mapping.items():
                if child_key in {"type", "code", "error", "message", "status"}:
                    if not isinstance(child_key, str):
                        continue
                    visit(child_value, key=child_key)
        elif isinstance(value, str) and key in {"type", "code", "message", "status"}:
            values.append(value)

    visit(payload)
    return tuple(values)
