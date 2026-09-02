"""Upstream failure observation, classification, and error mapping.

Extracted from ``RequestCoordinator`` in Plan 136 Phase 5.  These
functions normalize upstream failures into typed observations,
classify them into retry/effects decisions, and map the result
to the public upstream error hierarchy.

Design rules
~~~~~~~~~~~~
- ``build_failure_observation`` is the single normalization point for
  one upstream failure.  It never retains raw wire data.
- ``classify_upstream_error`` returns ``None`` for non-retryable
  client errors (400, non-model-specific 404) where the response
  body should be passed through as-is.
- ``error_from_failure_effects`` adapts the canonical decision table
  to the public upstream error hierarchy.
"""

from __future__ import annotations

import logging
from typing import Any

from eggpool.errors import (
    AuthenticationError,
    ModelUnavailableError,
    QuotaExhaustedError,
    RateLimitError,
    TemporaryUpstreamError,
    TransientUpstreamError,
    UpstreamError,
)
from eggpool.failure.classifier import classify_failure_effects
from eggpool.failure.signal_extract import extract_failure_signal
from eggpool.retry.classification import RetryClassifier

logger = logging.getLogger(__name__)


def build_failure_observation(
    *,
    context: Any | None,  # ProxyRequestContext  # noqa: ANN401
    selected: Any | None,  # SelectedAttempt  # noqa: ANN401
    status_code: int | None,
    headers: list[tuple[str, str]] | None = None,
    body: bytes | None = None,
    error_class: str | None = None,
    source: str = "upstream_http",
    response_started: bool = False,
    downstream_started: bool = False,
    credential_configured: bool = False,
    alternate_wire_available: bool = False,
    dispatch_phase: str = "response_status",
) -> Any:  # FailureObservation
    """Normalize one upstream failure without retaining raw wire data."""
    from eggpool.failure import FailureObservation

    classifier = RetryClassifier()
    header_map = {key.lower(): value for key, value in (headers or [])}
    retry_after = classifier.parse_retry_after(
        header_map,
        default=None,
    )
    return FailureObservation(
        source=source,
        status_code=status_code,
        error_class=error_class,
        provider_id=selected.provider_id if selected is not None else None,
        account_name=selected.account_name if selected is not None else None,
        model_id=context.model_id if context is not None else None,
        upstream_model_id=context.model_id if context is not None else None,
        client_protocol=context.protocol if context is not None else "openai",
        upstream_protocol=(
            context.upstream_protocol if context is not None else "openai"
        ),
        response_signal=extract_failure_signal(
            body,
            error_class=error_class,
            status_code=status_code,
            credential_configured=credential_configured,
            alternate_wire_available=alternate_wire_available,
        ),
        retry_after_s=retry_after,
        response_started=response_started,
        proxy_request_id=context.request_id if context is not None else None,
        attempt_id=selected.attempt_id if selected is not None else None,
        downstream_started=downstream_started,
        credential_configured=credential_configured,
        alternate_wire_available=alternate_wire_available,
        dispatch_phase=dispatch_phase,
    )


def error_from_failure_effects(
    effects: Any,  # FailureEffects  # noqa: ANN401
    *,
    status_code: int | None,
) -> UpstreamError | None:
    """Adapt the canonical decision to the public upstream errors."""
    if effects.account_effect == "disable_auth":
        return AuthenticationError("Authentication failed", status_code=status_code)
    if effects.account_effect == "rate_limit":
        return RateLimitError(
            "Rate limited",
            status_code=status_code,
            retry_after=(
                effects.retry_after_s if effects.retry_after_s is not None else 60.0
            ),
        )
    if effects.account_effect == "quota":
        return QuotaExhaustedError("Quota exhausted", status_code=status_code)
    if effects.model_effect != "none":
        return ModelUnavailableError("Model unavailable", status_code=status_code)
    if effects.account_effect in {"cooldown", "failure"}:
        if status_code in {408, 502, 504}:
            return TransientUpstreamError(
                effects.evidence_class,
                status_code=status_code,
            )
        return TemporaryUpstreamError(
            effects.evidence_class,
            status_code=status_code,
        )
    if effects.retry:
        return TemporaryUpstreamError(
            effects.evidence_class,
            status_code=status_code,
        )
    return None


def classify_upstream_failure(
    *,
    context: Any,  # ProxyRequestContext  # noqa: ANN401
    selected: Any,  # SelectedAttempt  # noqa: ANN401
    status_code: int,
    headers: list[tuple[str, str]],
    body: bytes | None,
) -> tuple[UpstreamError | None, Any, Any]:
    """Classify an upstream response once for retry and shared effects.

    Returns ``(error, observation, effects)`` where ``error`` is
    ``None`` for non-retryable client errors.
    """
    observation = build_failure_observation(
        context=context,
        selected=selected,
        status_code=status_code,
        headers=headers,
        body=body,
    )
    effects = classify_failure_effects(observation)
    return (
        error_from_failure_effects(effects, status_code=status_code),
        observation,
        effects,
    )


def classify_upstream_error(
    status_code: int,
    headers: list[tuple[str, str]],
    body: bytes | None = None,
) -> UpstreamError | None:
    """Classify an upstream error status code into an exception.

    Returns None for non-retryable client errors (400, non-model-specific 404)
    where the response body should be passed through as-is.
    """
    observation = build_failure_observation(
        context=None,
        selected=None,
        status_code=status_code,
        headers=headers,
        body=body,
    )
    return error_from_failure_effects(
        classify_failure_effects(observation),
        status_code=status_code,
    )
