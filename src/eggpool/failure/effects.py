"""Failure effects — immutable output from the effects classifier."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FailureEffects:
    """Immutable decision produced by the pure effects classifier.

    Every coordinator/finalizer/health call site consumes one of these
    rather than independently reclassifying status and error class.
    The mandatory default for unknown validation is zero shared-state
    effects with only a probe-slot release.
    """

    retry: bool
    """Whether the request may be retried on a different account."""

    retry_scope: str
    """``none``, ``same_account``, or ``other_account``."""

    client_outcome: str
    """``client_error``, ``upstream_error``, ``service_unavailable``,
    or ``timeout``."""

    account_effect: str
    """``none``, ``failure``, ``cooldown``, ``quota``, ``rate_limit``,
    or ``disable_auth``."""

    model_effect: str
    """``none``, ``quarantine``, or ``terminal_withdrawal``."""

    circuit_penalty: bool
    """Whether the circuit breaker should count this as a failure."""

    persist_backoff: bool
    """Whether durable backoff should be written to SQLite."""

    backoff_reason: str | None
    """The reason string for persistent backoff, or ``None``."""

    backoff_until: float | None
    """POSIX epoch for the backoff expiry, or ``None`` for terminal /
    no-backoff cases."""

    release_probe_only: bool
    """When ``True`` the only shared-state effect is releasing the
    half-open probe slot (client cancellation, client validation)."""

    evidence_class: str
    """Human-readable classification of the evidence that drove this
    decision, for diagnostics and routing traces."""
