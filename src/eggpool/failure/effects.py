"""Failure effects — immutable output from the effects classifier."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eggpool.failure.signal import FailureSignal


@dataclass(frozen=True, slots=True)
class FailureEffects:
    """Immutable decision produced by the pure effects classifier.

    Every coordinator/finalizer/health call site consumes one of these
    rather than independently reclassifying status and error class.
    The mandatory default for unknown validation is zero shared-state
    effects with only a probe-slot release.
    """

    retry: bool
    """Whether the request may be retried at the selected destination."""

    retry_scope: str
    """Compatibility scope: ``none``, ``same_account_other_wire``, or
    ``other_account``."""

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

    # The following fields make this object the complete immutable decision
    # carried from the failure boundary through retry, cleanup, and
    # finalization.  Defaults keep the Plan 025 constructor surface usable by
    # compatibility callers while production classifiers populate them.
    circuit_transition: str = "none"
    """``none``, ``success``, or ``failure`` for the circuit component."""

    probe_convergence: str = "release"
    """How the selected attempt must converge its probe slot."""

    provider_attributable: bool = False
    """Whether the decision permits provider/account consequences."""

    source: str = "unknown"
    response_signal: FailureSignal | None = None
    retry_after_s: float | None = None
    retry_action: str = "none"
    """Canonical retry destination."""

    wire_effect: str = "none"
    """Canonical wire-cache effect: ``none`` or ``reject_candidate``."""


FailureDecision = FailureEffects
"""Compatibility name for the canonical combined retry/effects decision."""
