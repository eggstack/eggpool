"""Canonical failure observation — immutable input for effects classification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eggpool.failure.signal import FailureSignal


@dataclass(frozen=True, slots=True)
class FailureObservation:
    """Immutable input record describing a single failure event.

    Every coordinator/finalizer/health call site constructs one of
    these to feed the pure effects classifier.  The observation
    captures all context needed to decide retry scope, account/model
    effects, circuit penalties, and durable backoff — without storing
    or propagating raw response bodies.
    """

    source: str
    """Origin of the failure: ``client_validation``, ``provider_validation``,
    ``upstream_http``, ``transport``, ``stream``, ``finalization``,
    or ``database``."""

    status_code: int | None
    """HTTP status code from the upstream response, or ``None`` for
    non-HTTP failures."""

    error_class: str | None
    """Structured error class from the upstream response body, or
    ``None`` when the response did not include one."""

    provider_id: str | None
    """Provider that produced the failure, or ``None`` when unknown."""

    account_name: str | None
    """Account used for the attempt, or ``None`` for client-side
    failures before account selection."""

    model_id: str | None
    """Canonical (collapsed) model ID requested by the client."""

    upstream_model_id: str | None
    """Model ID as sent to / returned by the upstream provider."""

    client_protocol: str
    """Protocol the client used (``openai`` or ``anthropic``)."""

    upstream_protocol: str
    """Protocol the upstream used (``openai`` or ``anthropic``)."""

    response_signal: FailureSignal | None
    """Conservatively extracted signal from the response body / JSON
    fields.  ``None`` when no signal could be extracted."""

    retry_after_s: float | None
    """Parsed ``Retry-After`` value in seconds, or ``None``."""

    response_started: bool
    """``True`` when at least one byte was received from upstream.
    Client cancellation after response start may differ from
    cancellation before any bytes."""
