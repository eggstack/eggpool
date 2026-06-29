"""Reason-specific backoff policies for upstream-observed failures.

Provides :class:`BackoffPolicy`, :class:`BackoffReason`, and helpers that
translate a normalized failure category into a bounded exponential delay
with jitter. Policies are intentionally kept separate from
:class:`eggpool.health.health_manager.HealthManager` so the mapping can
be unit-tested and reviewed in isolation, and so future policies can be
introduced (per provider, per model family) without touching health
state code.

Design rules:

* Local-estimate quota overage MUST NOT create a backoff. Only
  provider-observed failures (``quota_exhausted``, ``rate_limited``,
  ``upstream_server_error``, transport errors, ``model_unavailable``,
  ``authentication_failed``) are routed through this module.
* Auth failures are terminal. The health manager handles them with an
  indefinite operator reset rather than exponential backoff; this
  module returns ``None`` so callers skip the exponential path.
* Context-limit and pure client 4xx errors are not backoff-eligible.
* Backoff is bounded: ``cap`` is the maximum delay regardless of how
  many consecutive failures stack up.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)


class BackoffReason(StrEnum):
    """Normalized failure categories eligible for backoff.

    Mirrors :class:`eggpool.health.health_manager.FailureCategory` but
    is intentionally a separate enum so the backoff layer can evolve
    independently of the health classifier.
    """

    AUTHENTICATION_FAILED = "authentication_failed"
    QUOTA_EXHAUSTED = "quota_exhausted"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_SERVER_ERROR = "upstream_server_error"
    CONNECT_TIMEOUT = "connect_timeout"
    CONNECTION_FAILURE = "connection_failure"
    PROTOCOL_ERROR = "protocol_error"
    MODEL_UNAVAILABLE = "model_unavailable"
    CONTEXT_LIMIT_EXCEEDED = "context_limit_exceeded"


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Reason-specific backoff policy for upstream-observed failures.

    ``base_delay`` is the first-failure delay. Each consecutive failure
    multiplies the delay by ``multiplier`` until ``cap`` is reached.
    ``jitter`` (0.0-1.0) is a uniform multiplicative noise factor
    applied to the final value. ``scope`` identifies whether the
    backoff applies to the whole account or to a specific account/model
    pair; the health manager is responsible for translating scope into
    state.
    """

    base_delay: float
    multiplier: float
    cap: float
    jitter: float
    scope: str
    max_consecutive: int = 6


def _jitterize(value: float, jitter: float) -> float:
    """Return ``value`` perturbed by uniform multiplicative jitter.

    ``jitter=0.15`` produces a value in ``[value * 0.85, value * 1.15]``.
    Negative or invalid jitter values are treated as zero so the
    exponential schedule remains well-defined.
    """
    if jitter <= 0.0 or value <= 0.0:
        return value
    low = max(0.0, 1.0 - jitter)
    high = 1.0 + jitter
    return value * random.uniform(low, high)


def get_backoff_policy(reason: str) -> BackoffPolicy | None:
    """Return the policy for a backoff reason, or ``None`` if no policy.

    The mapping follows the backoff policy design. Reasons without a
    policy (``context_limit_exceeded`` and any unknown value) return
    ``None``; the caller must not apply any cooldown in that case.
    """
    try:
        r = BackoffReason(reason)
    except ValueError:
        return None

    if r is BackoffReason.AUTHENTICATION_FAILED:
        # Terminal. The health manager disables the account until an
        # operator reset; no exponential backoff applies.
        return BackoffPolicy(
            base_delay=0.0,
            multiplier=1.0,
            cap=0.0,
            jitter=0.0,
            scope="account",
            max_consecutive=0,
        )
    if r is BackoffReason.QUOTA_EXHAUSTED:
        return BackoffPolicy(
            base_delay=300.0,  # 5 minutes
            multiplier=2.0,
            cap=86400.0,  # 24 hours
            jitter=0.15,
            scope="account",
        )
    if r is BackoffReason.RATE_LIMITED:
        # Retry-After (when present) takes precedence over the
        # exponential base; see ``compute_backoff_seconds``.
        return BackoffPolicy(
            base_delay=60.0,
            multiplier=2.0,
            cap=86400.0,
            jitter=0.15,
            scope="account",
        )
    if r is BackoffReason.UPSTREAM_SERVER_ERROR:
        return BackoffPolicy(
            base_delay=20.0,  # 15-30s midpoint
            multiplier=2.0,
            cap=1800.0,  # 30 minutes
            jitter=0.15,
            scope="account",
        )
    if r in (
        BackoffReason.CONNECT_TIMEOUT,
        BackoffReason.CONNECTION_FAILURE,
        BackoffReason.PROTOCOL_ERROR,
    ):
        return BackoffPolicy(
            base_delay=30.0,
            multiplier=2.0,
            cap=1800.0,
            jitter=0.15,
            scope="account",
        )
    if r is BackoffReason.MODEL_UNAVAILABLE:
        return BackoffPolicy(
            base_delay=300.0,
            multiplier=2.0,
            cap=86400.0,
            jitter=0.15,
            scope="account_model",
        )
    if r is BackoffReason.CONTEXT_LIMIT_EXCEEDED:
        # No account-level suppression for context-limit errors.
        return None

    return None


def compute_backoff_seconds(
    reason: str,
    consecutive_failures: int,
    retry_after: float | None = None,
    *,
    jitter: bool = True,
    now: float | None = None,
) -> float | None:
    """Return the bounded backoff delay for a failure, or ``None``.

    Parameters
    ----------
    reason:
        A :class:`BackoffReason` string. Unknown or unsupported
        categories return ``None`` so the caller can skip cooldown.
    consecutive_failures:
        Number of consecutive failures observed for this account (or
        account/model scope). Used to grow the delay exponentially.
    retry_after:
        Optional upstream ``Retry-After`` value in seconds. When
        supplied and the policy is ``rate_limited`` or
        ``quota_exhausted``, it overrides the exponential base so a
        provider's explicit wait is honored.
    jitter:
        Apply the policy's jitter factor. Default ``True``; set to
        ``False`` for deterministic testing.
    now:
        Wall-clock reference for jitter random seeding only. Defaults
        to ``time.time()`` so multiple calls in the same second are
        stable enough for tests.
    """
    policy = get_backoff_policy(reason)
    if policy is None or policy.base_delay <= 0 or policy.cap <= 0:
        return None

    # Honor upstream Retry-After before computing exponential growth.
    # The provider's explicit cooldown is always at least as trustworthy
    # as our local schedule.
    if (
        retry_after is not None
        and retry_after >= 0
        and reason
        in (
            BackoffReason.RATE_LIMITED.value,
            BackoffReason.QUOTA_EXHAUSTED.value,
        )
    ):
        delay = min(float(retry_after), policy.cap)
        if jitter:
            delay = _jitterize(delay, policy.jitter)
        return max(0.0, delay)

    if consecutive_failures <= 1:
        delay = policy.base_delay
    else:
        # Cap the exponent explicitly so pathological counters cannot
        # overflow or exceed ``max_consecutive`` doublings.
        doublings = max(
            0, min(consecutive_failures - 1, max(policy.max_consecutive, 0))
        )
        delay = policy.base_delay * (policy.multiplier**doublings)
        if delay > policy.cap or delay <= 0:
            delay = policy.cap

    if jitter:
        delay = _jitterize(delay, policy.jitter)

    return min(max(0.0, delay), policy.cap)


def is_backoff_reason(reason: str) -> bool:
    """Return whether the reason has any backoff policy at all.

    Useful as a guard at call sites: callers can skip cooldown logic
    entirely when the reason maps to ``None``.
    """
    return get_backoff_policy(reason) is not None


def seed_random_for_test(seed: int) -> None:
    """Seed the jitter RNG for deterministic testing.

    Module-level helper so tests can produce stable delays without
    monkey-patching ``random``. Does not affect production code paths
    where seeding is intentionally left to system entropy.
    """
    random.seed(seed)
