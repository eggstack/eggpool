"""Response signals extracted from upstream failures.

Signals are conservative, bounded extractions from response bodies and
structured JSON fields.  Raw response bodies are never stored or
propagated in observations.
"""

from __future__ import annotations

from enum import StrEnum


class FailureSignal(StrEnum):
    """Normalized signals extracted from upstream failure responses.

    Extraction may inspect a bounded response prefix and structured
    JSON fields, then discard content.  Signals are used by the effects
    classifier to disambiguate ambiguous HTTP status codes (e.g. 403
    with quota signal vs. 403 without evidence).
    """

    CREDENTIAL_INVALID = "credential_invalid"
    # Compatibility alias for the former vocabulary.
    AUTHENTICATION_FAILED = "credential_invalid"
    WIRE_AUTH_MISMATCH = "wire_auth_mismatch"
    WIRE_SURFACE_UNSUPPORTED = "wire_surface_unsupported"
    WIRE_SCHEMA_MISMATCH = "wire_schema_mismatch"
    QUOTA_EXHAUSTED = "quota_exhausted"
    RATE_LIMITED = "rate_limited"
    MODEL_ABSENT = "model_absent"
    UNSUPPORTED_REQUEST_CONTROL = "unsupported_request_control"
    CONTEXT_LIMIT_EXCEEDED = "context_limit_exceeded"
    GENERIC_CLIENT_VALIDATION = "generic_client_validation"
    TEMPORARY_UPSTREAM_FAILURE = "temporary_upstream_failure"
    TRANSPORT_FAILURE = "transport_failure"
    UNKNOWN = "unknown"
