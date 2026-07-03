"""Phase 10 closed-loop threshold tuning engine.

Phase 10 is currently recommendation-only.  ``mode = "apply"`` is
accepted at config time but does not currently register runtime
overrides -- a future supervised background task must call
``build_runtime_override()`` then ``registry.register()`` before
apply mode takes effect.  Until then, ``compute_recommendation``
always tags recommendations as ``recommendation_only``.

The tuning engine analyses a per-policy window of compression
observations and produces bounded recommendations to adjust
``min_candidate_tokens``, ``min_savings_tokens``, and
``max_compression_latency_ms`` within operator-defined bounds.

Core invariants:

- The engine is **content-private**.  It only ever reads the
  precomputed aggregate columns on the ``requests`` table plus the
  in-memory ``CompressionPolicyContext``.  It never reads raw prompt
  text, tool outputs, system messages, or any auth header.
- The engine never tunes the following fields, even when the global
  ``mode = "apply"`` is enabled: ``enabled``, ``mode``,
  ``placement``, ``respect_cache_boundaries``, ``compress_static_prefix``,
  and synthetic cache-control knobs.  These are routing, safety, or
  scope decisions that only the operator may make.
- Every recommendation is clamped to the operator-defined bounds and
  bounded by ``max_adjustment_pct`` per step.
- Recommendations are explainable: the output includes a stable
  ``reason_codes`` tuple so dashboards and operators can audit
  exactly which heuristic fired.
- The engine never raises in a request path.  All inputs are
  defensive.

Public surface:

- :class:`TuningWindowMetrics` -- per-policy aggregate window data.
- :class:`CompressionTuningRecommendation` -- immutable output.
- :class:`RuntimeCompressionPolicyOverride` -- runtime overlay for
  future apply mode.  Currently unused outside tests.
- :func:`compute_recommendation` -- pure function from inputs to
  recommendation.
- :func:`clamp_int`, :func:`clamp_float`, :func:`clamp_step` --
  helpers used by tests and the dashboard.

The engine never imports from ``stats.queries`` or any DB layer.  All
metrics are precomputed and passed in.  The DB-aggregation step lives
in :mod:`eggpool.stats.queries.fetch_compression_tuning_window_metrics`
so this module stays side-effect-free.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from eggpool.transcoder.compression.policy import (
        CompressionConfig,
        CompressionTuningBoundsConfig,
        CompressionTuningConfig,
        CompressionTuningTargetsConfig,
    )


# ---------------------------------------------------------------------------
# Reason codes (stable strings surfaced in the API + dashboard)
# ---------------------------------------------------------------------------

REASON_INSUFFICIENT_DATA: str = "insufficient_data"
REASON_HIGH_LATENCY_WARNING_RATE: str = "high_latency_warning_rate"
REASON_HIGH_FALLBACK_RATE: str = "high_fallback_rate"
REASON_LOW_POSITIVE_SAVINGS_RATE: str = "low_positive_savings_rate"
REASON_BELOW_CANDIDATE_THRESHOLD_HIGH: str = (
    "below_candidate_threshold_suppression_high"
)
REASON_BELOW_SAVINGS_THRESHOLD_HIGH: str = "below_savings_threshold_suppression_high"
REASON_STRONG_SAVINGS_LOW_LATENCY: str = "strong_savings_low_latency"
REASON_COOLDOWN_ACTIVE: str = "cooldown_active"
REASON_BOUNDED_BY_MIN: str = "bounded_by_min"
REASON_BOUNDED_BY_MAX: str = "bounded_by_max"
REASON_RECOMMENDATION_ONLY: str = "recommendation_only"
REASON_APPLIED_RUNTIME_OVERRIDE: str = "applied_runtime_override"

# Reason codes that indicate a safety rail triggered. Surfaced
# separately in the dashboard so operators see the "Safety blockers"
# list without filtering themselves.
_SAFETY_BLOCKER_REASONS: frozenset[str] = frozenset(
    {
        REASON_HIGH_FALLBACK_RATE,
        REASON_HIGH_LATENCY_WARNING_RATE,
        REASON_BOUNDED_BY_MIN,
        REASON_BOUNDED_BY_MAX,
    },
)

# Threshold fields the engine is allowed to tune.  Any field not in
# this set is off-limits to the engine; the resolver and policy
# validator both enforce this.
TUNABLE_FIELDS: frozenset[str] = frozenset(
    {
        "min_candidate_tokens",
        "min_savings_tokens",
        "max_compression_latency_ms",
    },
)


# ---------------------------------------------------------------------------
# Inputs and outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TuningWindowMetrics:
    """Aggregate compression metrics for one policy window.

    All counts are >= 0.  ``latency_budget_warning_count`` and
    ``failed_fallback_count`` are sub-counts of ``total_requests``;
    the engine computes rates itself to avoid caller arithmetic.

    The engine never inspects raw prompts or transformed text.  These
    fields are exactly the persisted aggregate columns populated by
    :mod:`eggpool.transcoder.compression.analyzer` and
    :mod:`eggpool.transcoder.compression.apply` finalizers.
    """

    total_requests: int
    applied_count: int
    suppressed_count: int
    below_min_candidate_count: int
    below_min_savings_count: int
    latency_budget_warning_count: int
    failed_fallback_count: int
    positive_savings_count: int
    p95_latency_ms: float
    median_latency_ms: float
    median_savings_tokens: float
    p95_savings_tokens: float

    @classmethod
    def empty(cls) -> TuningWindowMetrics:
        """Return a zero-valued metrics instance for missing data."""
        return cls(
            total_requests=0,
            applied_count=0,
            suppressed_count=0,
            below_min_candidate_count=0,
            below_min_savings_count=0,
            latency_budget_warning_count=0,
            failed_fallback_count=0,
            positive_savings_count=0,
            p95_latency_ms=0.0,
            median_latency_ms=0.0,
            median_savings_tokens=0.0,
            p95_savings_tokens=0.0,
        )


@dataclass(frozen=True, slots=True)
class CompressionTuningRecommendation:
    """Immutable output of :func:`compute_recommendation`.

    ``status`` is one of:

    - ``"insufficient_data"``: window smaller than ``min_window_requests``.
    - ``"recommended"``: at least one tunable was recommended.
    - ``"suppressed"``: cooldown active or no change suggested.

    ``mode = "apply"`` is accepted at config time for forward
    compatibility but no background task currently wires
    recommendations into the runtime override registry.  All
    recommendations are tagged ``"recommended"`` regardless of mode.
    """

    policy_name: str
    status: str
    window_request_count: int
    current: dict[str, float]
    recommended: dict[str, float]
    reason_codes: tuple[str, ...]
    metrics: dict[str, float]
    safety_blockers: tuple[str, ...]
    generated_at: datetime
    generated_reason_codes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        """Compact dict for the persistence + dashboard layer."""
        return {
            "policy_name": self.policy_name,
            "status": self.status,
            "window_request_count": int(self.window_request_count),
            "current": dict(self.current),
            "recommended": dict(self.recommended),
            "reason_codes": list(self.reason_codes),
            "metrics": dict(self.metrics),
            "safety_blockers": list(self.safety_blockers),
            "generated_at": self.generated_at.isoformat(),
            "generated_reason_codes": list(self.generated_reason_codes),
        }


@dataclass(frozen=True, slots=True)
class RuntimeCompressionPolicyOverride:
    """In-memory runtime override produced by ``mode = "apply"``.

    This object lives only in the registry; it is never persisted
    back into the operator's config file.  ``expires_at`` is advisory
    and used by the resolver to drop the override after the
    ``cooldown_s`` window passes.
    """

    policy_name: str
    fields: Mapping[str, int | float]
    generated_at: datetime
    expires_at: datetime | None
    reason_codes: tuple[str, ...]


# ---------------------------------------------------------------------------
# Clamp helpers
# ---------------------------------------------------------------------------


def clamp_int(value: int, lo: int, hi: int) -> int:
    """Clamp an int to ``[lo, hi]`` and round floats defensively."""
    if isinstance(value, float) and math.isnan(value):
        value = 0
    value = int(round(value))
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def clamp_float(value: float, lo: float, hi: float) -> float:
    """Clamp a float to ``[lo, hi]`` and coerce NaN to ``lo``."""
    if isinstance(value, float) and math.isnan(value):
        return lo
    if value < lo:
        return lo
    if value > hi:
        return hi
    return float(value)


def clamp_step(
    current: float,
    suggested: float,
    *,
    max_adjustment_pct: float,
    lo: float,
    hi: float,
) -> tuple[float, tuple[str, ...]]:
    """Clamp ``suggested`` to a step no larger than ``max_adjustment_pct``.

    The step size is computed against ``current`` (the existing
    threshold) and never moves more than ``max_adjustment_pct`` in
    either direction.  Returns ``(clamped_value, reasons)`` where
    ``reasons`` may include ``bounded_by_min`` or ``bounded_by_max``
    when the bounds kicked in.

    ``max_adjustment_pct`` of 0 is treated as no-op; ``100`` lets the
    suggestion move all the way to the bound in one step.
    """
    reasons: list[str] = []
    if max_adjustment_pct <= 0:
        return float(current), tuple(reasons)
    pct = max_adjustment_pct / 100.0
    delta = suggested - current
    max_delta = abs(current) * pct
    if delta > max_delta:
        suggested = current + max_delta
    elif delta < -max_delta:
        suggested = current - max_delta
    bounded_lo = False
    bounded_hi = False
    if suggested < lo:
        suggested = lo
        bounded_lo = True
    if suggested > hi:
        suggested = hi
        bounded_hi = True
    if bounded_lo:
        reasons.append(REASON_BOUNDED_BY_MIN)
    if bounded_hi:
        reasons.append(REASON_BOUNDED_BY_MAX)
    return float(suggested), tuple(reasons)


# ---------------------------------------------------------------------------
# Recommendation algorithm
# ---------------------------------------------------------------------------


def _safe_div(num: float, denom: float) -> float:
    """Defensive division; ``denom == 0`` returns 0.0."""
    if denom <= 0:
        return 0.0
    return float(num) / float(denom)


def _metrics_to_dict(metrics: TuningWindowMetrics) -> dict[str, float]:
    """Flatten a :class:`TuningWindowMetrics` for the recommendation payload.

    Rates are computed against the right denominator per the plan:

    - ``latency_budget_warning_rate`` and ``failed_fallback_rate``
      divide by ``total_requests`` (they are guard-rail rates over
      the whole window).
    - ``positive_savings_rate`` divides by ``applied_count`` (it
      measures the fraction of *applied* requests that produced
      positive savings; per the Phase 10 plan).
    """
    total = max(int(metrics.total_requests), 0)
    applied = max(int(metrics.applied_count), 0)
    return {
        "total_requests": float(total),
        "applied_count": float(applied),
        "suppressed_count": float(max(int(metrics.suppressed_count), 0)),
        "below_min_candidate_count": float(
            max(int(metrics.below_min_candidate_count), 0),
        ),
        "below_min_savings_count": float(
            max(int(metrics.below_min_savings_count), 0),
        ),
        "latency_budget_warning_rate": _safe_div(
            metrics.latency_budget_warning_count,
            total,
        ),
        "failed_fallback_rate": _safe_div(metrics.failed_fallback_count, total),
        "positive_savings_rate": _safe_div(metrics.positive_savings_count, applied),
        "p95_latency_ms": float(metrics.p95_latency_ms),
        "median_latency_ms": float(metrics.median_latency_ms),
        "median_savings_tokens": float(metrics.median_savings_tokens),
        "p95_savings_tokens": float(metrics.p95_savings_tokens),
    }


def _current_thresholds(config: CompressionConfig) -> dict[str, float]:
    """Snapshot the three thresholds the engine may tune."""
    return {
        "min_candidate_tokens": float(config.min_candidate_tokens),
        "min_savings_tokens": float(config.min_savings_tokens),
        "max_compression_latency_ms": float(config.max_compression_latency_ms),
    }


def _empty_recommendation(
    *,
    policy_name: str,
    status: str,
    window_request_count: int,
    current: dict[str, float],
    reason_codes: tuple[str, ...],
    metrics: dict[str, float],
    generated_at: datetime,
    generated_reason_codes: tuple[str, ...] = (),
) -> CompressionTuningRecommendation:
    """Build a no-op recommendation with ``recommended == current``."""
    return CompressionTuningRecommendation(
        policy_name=policy_name,
        status=status,
        window_request_count=int(window_request_count),
        current=dict(current),
        recommended=dict(current),
        reason_codes=reason_codes,
        metrics=metrics,
        safety_blockers=tuple(
            code for code in reason_codes if code in _SAFETY_BLOCKER_REASONS
        ),
        generated_at=generated_at,
        generated_reason_codes=generated_reason_codes,
    )


def compute_recommendation(
    *,
    policy_name: str,
    config: CompressionConfig,
    metrics: TuningWindowMetrics,
    tuning_config: CompressionTuningConfig,
    last_recommendation_at: datetime | None = None,
    now: datetime | None = None,
) -> CompressionTuningRecommendation:
    """Compute a tuning recommendation for one policy window.

    Algorithm (per the plan):

    1. If ``metrics.total_requests < tuning.min_window_requests``,
       return ``insufficient_data``.
    2. If ``last_recommendation_at`` is within ``cooldown_s``,
       return ``suppressed`` with ``cooldown_active``.
    3. If ``failed_fallback_rate > max_failed_fallback_rate``,
       recommend raising ``min_candidate_tokens`` and
       ``min_savings_tokens`` (never lower them); emit
       ``high_fallback_rate``.
    4. If ``latency_budget_warning_rate > max_latency_budget_warning_rate``
       OR ``p95_latency_ms > max_p95_latency_ms``, recommend raising
       ``min_candidate_tokens``; emit ``high_latency_warning_rate``.
    5. If ``positive_savings_rate < min_positive_savings_rate``,
       recommend raising ``min_savings_tokens``; emit
       ``low_positive_savings_rate``.
    6. If many requests were suppressed by ``below_min_candidate``,
       recommend lowering ``min_candidate_tokens`` by at most
       ``max_adjustment_pct``; emit
       ``below_candidate_threshold_suppression_high``.
    7. If safe applied requests consistently show high savings and
       low latency, recommend modestly lowering thresholds; emit
       ``strong_savings_low_latency``.
    8. If no heuristic fired, return ``suppressed`` with empty
       reasons.
    9. Every suggestion is clamped to bounds and step-limited.

    Returns a :class:`CompressionTuningRecommendation` whose
    ``status`` is ``"insufficient_data"`` / ``"recommended"`` /
    ``"suppressed"``.  ``mode = "apply"`` is accepted at config time
    for forward compatibility, but no background task currently wires
    recommendations into the ``RuntimeCompressionPolicyOverrideRegistry``.
    Until a future lifecycle task is added, every recommendation is
    advisory regardless of mode.

    The function is pure: no I/O, no clock reads, no logging.  The
    caller passes ``now`` and the persisted ``last_recommendation_at``
    for deterministic test runs.
    """
    # Local imports avoid circular import during package wiring.
    from eggpool.transcoder.compression.policy import (
        CompressionTuningConfig,
    )

    assert isinstance(tuning_config, CompressionTuningConfig)
    targets: CompressionTuningTargetsConfig = tuning_config.targets
    bounds: CompressionTuningBoundsConfig = tuning_config.bounds
    generated_at = now if now is not None else datetime.now(UTC)
    current = _current_thresholds(config)
    metrics_dict = _metrics_to_dict(metrics)
    window_count = int(metrics.total_requests)

    # Insufficient data short-circuits with no recommended change.
    if window_count < tuning_config.min_window_requests:
        return _empty_recommendation(
            policy_name=policy_name,
            status="insufficient_data",
            window_request_count=window_count,
            current=current,
            reason_codes=(REASON_INSUFFICIENT_DATA,),
            metrics=metrics_dict,
            generated_at=generated_at,
        )

    # Cooldown suppresses oscillating changes.
    if last_recommendation_at is not None:
        cooldown_s = int(tuning_config.cooldown_s)
        elapsed = (generated_at - last_recommendation_at).total_seconds()
        if 0 <= elapsed < cooldown_s:
            return _empty_recommendation(
                policy_name=policy_name,
                status="suppressed",
                window_request_count=window_count,
                current=current,
                reason_codes=(REASON_COOLDOWN_ACTIVE,),
                metrics=metrics_dict,
                generated_at=generated_at,
            )

    # Step 1: collect suggested values per heuristic.  We start from
    # the current values and only modify when a heuristic fires.
    suggested: dict[str, float] = dict(current)
    reasons: list[str] = []

    # Step 3: high fallback rate -- raise thresholds (never lower).
    if metrics_dict["failed_fallback_rate"] > targets.max_failed_fallback_rate:
        reasons.append(REASON_HIGH_FALLBACK_RATE)
        # Increase both thresholds by max_adjustment_pct, up to the
        # upper bound.  ``clamp_step`` enforces both step size and
        # bounds; the resulting reason codes may include
        # ``bounded_by_max``.
        step_min_candidate, step_reasons = clamp_step(
            current["min_candidate_tokens"],
            current["min_candidate_tokens"]
            * (1.0 + tuning_config.max_adjustment_pct / 100.0),
            max_adjustment_pct=tuning_config.max_adjustment_pct,
            lo=float(bounds.min_candidate_tokens_min),
            hi=float(bounds.min_candidate_tokens_max),
        )
        suggested["min_candidate_tokens"] = clamp_int(
            int(step_min_candidate),
            bounds.min_candidate_tokens_min,
            bounds.min_candidate_tokens_max,
        )
        for code in step_reasons:
            if code not in reasons:
                reasons.append(code)
        step_min_savings, step_reasons = clamp_step(
            current["min_savings_tokens"],
            current["min_savings_tokens"]
            * (1.0 + tuning_config.max_adjustment_pct / 100.0),
            max_adjustment_pct=tuning_config.max_adjustment_pct,
            lo=float(bounds.min_savings_tokens_min),
            hi=float(bounds.min_savings_tokens_max),
        )
        suggested["min_savings_tokens"] = clamp_int(
            int(step_min_savings),
            bounds.min_savings_tokens_min,
            bounds.min_savings_tokens_max,
        )
        for code in step_reasons:
            if code not in reasons:
                reasons.append(code)

    # Step 4: high latency warning rate OR high p95 latency.
    if (
        metrics_dict["latency_budget_warning_rate"]
        > targets.max_latency_budget_warning_rate
        or metrics_dict["p95_latency_ms"] > targets.max_p95_latency_ms
    ):
        if REASON_HIGH_LATENCY_WARNING_RATE not in reasons:
            reasons.append(REASON_HIGH_LATENCY_WARNING_RATE)
        # Prefer raising thresholds over lowering the latency budget
        # so the analyzer can still see large candidates.
        step_min_candidate, step_reasons = clamp_step(
            current["min_candidate_tokens"],
            current["min_candidate_tokens"]
            * (1.0 + tuning_config.max_adjustment_pct / 100.0),
            max_adjustment_pct=tuning_config.max_adjustment_pct,
            lo=float(bounds.min_candidate_tokens_min),
            hi=float(bounds.min_candidate_tokens_max),
        )
        suggested["min_candidate_tokens"] = clamp_int(
            int(step_min_candidate),
            bounds.min_candidate_tokens_min,
            bounds.min_candidate_tokens_max,
        )
        for code in step_reasons:
            if code not in reasons:
                reasons.append(code)
        # Optionally tighten the latency budget when it's already
        # above the policy default.  We only lower it when current
        # > targets.max_p95_latency_ms * 2 to avoid oscillation.
        if current["max_compression_latency_ms"] > targets.max_p95_latency_ms * 2:
            new_budget = min(
                current["max_compression_latency_ms"] * 0.9,
                targets.max_p95_latency_ms,
            )
            step_budget, step_reasons = clamp_step(
                current["max_compression_latency_ms"],
                new_budget,
                max_adjustment_pct=tuning_config.max_adjustment_pct,
                lo=float(bounds.max_compression_latency_ms_min),
                hi=float(bounds.max_compression_latency_ms_max),
            )
            suggested["max_compression_latency_ms"] = clamp_float(
                step_budget,
                bounds.max_compression_latency_ms_min,
                bounds.max_compression_latency_ms_max,
            )
            for code in step_reasons:
                if code not in reasons:
                    reasons.append(code)

    # Step 5: low positive savings rate -- raise ``min_savings_tokens``.
    if (
        metrics.applied_count > 0
        and metrics_dict["positive_savings_rate"] < targets.min_positive_savings_rate
    ):
        if REASON_LOW_POSITIVE_SAVINGS_RATE not in reasons:
            reasons.append(REASON_LOW_POSITIVE_SAVINGS_RATE)
        step_min_savings, step_reasons = clamp_step(
            current["min_savings_tokens"],
            current["min_savings_tokens"]
            * (1.0 + tuning_config.max_adjustment_pct / 100.0),
            max_adjustment_pct=tuning_config.max_adjustment_pct,
            lo=float(bounds.min_savings_tokens_min),
            hi=float(bounds.min_savings_tokens_max),
        )
        suggested["min_savings_tokens"] = clamp_int(
            int(step_min_savings),
            bounds.min_savings_tokens_min,
            bounds.min_savings_tokens_max,
        )
        for code in step_reasons:
            if code not in reasons:
                reasons.append(code)

    # Step 6: many requests suppressed by below_min_candidate_tokens.
    # The plan recommends lowering ``min_candidate_tokens`` so more
    # candidates qualify.
    below_min_cand_threshold = max(
        int(metrics.total_requests) * 0.10,  # >=10% suppressed is significant
        int(tuning_config.min_window_requests),
    )
    if (
        int(metrics.below_min_candidate_count) >= below_min_cand_threshold
        and int(metrics.applied_count) > 0
    ):
        if REASON_BELOW_CANDIDATE_THRESHOLD_HIGH not in reasons:
            reasons.append(REASON_BELOW_CANDIDATE_THRESHOLD_HIGH)
        step_min_candidate, step_reasons = clamp_step(
            current["min_candidate_tokens"],
            current["min_candidate_tokens"]
            * (1.0 - tuning_config.max_adjustment_pct / 100.0),
            max_adjustment_pct=tuning_config.max_adjustment_pct,
            lo=float(bounds.min_candidate_tokens_min),
            hi=float(bounds.min_candidate_tokens_max),
        )
        suggested["min_candidate_tokens"] = clamp_int(
            int(step_min_candidate),
            bounds.min_candidate_tokens_min,
            bounds.min_candidate_tokens_max,
        )
        for code in step_reasons:
            if code not in reasons:
                reasons.append(code)

    # Step 7: strong savings + low latency -- modestly lower
    # thresholds so more candidates qualify.
    if (
        int(metrics.applied_count) > 0
        and metrics_dict["positive_savings_rate"] >= targets.min_positive_savings_rate
        and metrics_dict["p95_latency_ms"] <= targets.max_p95_latency_ms
        and metrics_dict["latency_budget_warning_rate"]
        <= targets.max_latency_budget_warning_rate
        and metrics_dict["failed_fallback_rate"] <= targets.max_failed_fallback_rate
        and metrics.median_savings_tokens >= targets.min_median_savings_tokens
    ):
        reasons.append(REASON_STRONG_SAVINGS_LOW_LATENCY)
        # Lower both thresholds by half of max_adjustment_pct so the
        # change is conservative even when savings are strong.
        conservative_pct = tuning_config.max_adjustment_pct / 2.0
        step_min_candidate, step_reasons = clamp_step(
            current["min_candidate_tokens"],
            current["min_candidate_tokens"] * (1.0 - conservative_pct / 100.0),
            max_adjustment_pct=conservative_pct,
            lo=float(bounds.min_candidate_tokens_min),
            hi=float(bounds.min_candidate_tokens_max),
        )
        suggested["min_candidate_tokens"] = clamp_int(
            int(step_min_candidate),
            bounds.min_candidate_tokens_min,
            bounds.min_candidate_tokens_max,
        )
        for code in step_reasons:
            if code not in reasons:
                reasons.append(code)
        step_min_savings, step_reasons = clamp_step(
            current["min_savings_tokens"],
            current["min_savings_tokens"] * (1.0 - conservative_pct / 100.0),
            max_adjustment_pct=conservative_pct,
            lo=float(bounds.min_savings_tokens_min),
            hi=float(bounds.min_savings_tokens_max),
        )
        suggested["min_savings_tokens"] = clamp_int(
            int(step_min_savings),
            bounds.min_savings_tokens_min,
            bounds.min_savings_tokens_max,
        )
        for code in step_reasons:
            if code not in reasons:
                reasons.append(code)

    # Step 8: nothing changed -> suppressed with no reasons.
    if suggested == current or not reasons:
        return _empty_recommendation(
            policy_name=policy_name,
            status="suppressed",
            window_request_count=window_count,
            current=current,
            reason_codes=(),
            metrics=metrics_dict,
            generated_at=generated_at,
        )

    # Step 9: tag with recommendation_only.  ``mode = "apply"`` is
    # accepted at config time for forward compatibility, but no
    # background task currently wires recommendations into the
    # RuntimeCompressionPolicyOverrideRegistry.  Until a future
    # lifecycle task is added, every recommendation is advisory.
    reasons_list = list(reasons) + [REASON_RECOMMENDATION_ONLY]
    return CompressionTuningRecommendation(
        policy_name=policy_name,
        status="recommended",  # always; never "applied"
        window_request_count=window_count,
        current=current,
        recommended=suggested,
        reason_codes=tuple(reasons_list),
        metrics=metrics_dict,
        safety_blockers=tuple(
            code for code in reasons_list if code in _SAFETY_BLOCKER_REASONS
        ),
        generated_at=generated_at,
        generated_reason_codes=tuple(reasons_list),
    )


def build_runtime_override(
    recommendation: CompressionTuningRecommendation,
    *,
    cooldown_s: int,
    now: datetime | None = None,
) -> RuntimeCompressionPolicyOverride:
    """Build a :class:`RuntimeCompressionPolicyOverride` from a recommendation.

    Only the fields in :data:`TUNABLE_FIELDS` are forwarded; the
    resolver double-checks this in tests and via the apply-mode
    guard.
    """
    generated_at = now if now is not None else datetime.now(UTC)
    expires_at = generated_at.fromtimestamp(
        generated_at.timestamp() + max(int(cooldown_s), 0),
        tz=UTC,
    )
    fields = {
        key: float(value)
        for key, value in recommendation.recommended.items()
        if key in TUNABLE_FIELDS
    }
    return RuntimeCompressionPolicyOverride(
        policy_name=recommendation.policy_name,
        fields=fields,
        generated_at=generated_at,
        expires_at=expires_at,
        reason_codes=recommendation.reason_codes,
    )


# ---------------------------------------------------------------------------
# In-memory override registry
# ---------------------------------------------------------------------------


class RuntimeCompressionPolicyOverrideRegistry:
    """Thread-safe in-memory registry for Phase 10 runtime overrides.

    The registry stores at most one :class:`RuntimeCompressionPolicyOverride`
    per policy.  Overrides expire after ``expires_at``; lookups drop
    expired entries and replace them on the next registration.

    Safety invariants:

    - Only fields in :data:`TUNABLE_FIELDS` are accepted; the resolver
      re-validates the merged config after applying each override.
    - The registry never writes to disk; the optional DB audit lives
      on :mod:`eggpool.stats.queries.upsert_compression_tuning_recommendation`.
    - The registry never inspects request content.
    """

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self._overrides: dict[str, RuntimeCompressionPolicyOverride] = {}

    def register(
        self,
        override: RuntimeCompressionPolicyOverride,
    ) -> None:
        """Store (or replace) the override for one policy.

        Fields outside :data:`TUNABLE_FIELDS` are silently dropped so a
        caller cannot bypass the safety rail.  This is a defence in
        depth: the resolver also rejects unknown fields.
        """
        safe_fields = {
            key: float(value)
            for key, value in override.fields.items()
            if key in TUNABLE_FIELDS
        }
        safe = RuntimeCompressionPolicyOverride(
            policy_name=override.policy_name,
            fields=safe_fields,
            generated_at=override.generated_at,
            expires_at=override.expires_at,
            reason_codes=override.reason_codes,
        )
        with self._lock:
            self._overrides[override.policy_name] = safe

    def lookup(
        self,
        policy_name: str,
        *,
        now: datetime | None = None,
    ) -> RuntimeCompressionPolicyOverride | None:
        """Return the active override for one policy.

        Expired entries are dropped on lookup.  ``None`` means "no
        active override" -- the resolver then uses the static
        resolved policy unchanged.
        """
        moment = now if now is not None else datetime.now(UTC)
        with self._lock:
            entry = self._overrides.get(policy_name)
            if entry is None:
                return None
            if entry.expires_at is not None and entry.expires_at <= moment:
                del self._overrides[policy_name]
                return None
            return entry

    def clear(self, policy_name: str | None = None) -> None:
        """Clear one policy override (or all of them).

        The registry is operator-clearable: an operator can call this
        from a CLI command (Phase 10 exposes a debug hook) without
        restarting the process.
        """
        with self._lock:
            if policy_name is None:
                self._overrides.clear()
                return
            self._overrides.pop(policy_name, None)

    def snapshot(self) -> dict[str, RuntimeCompressionPolicyOverride]:
        """Return a defensive snapshot of the registry (testing)."""
        with self._lock:
            return dict(self._overrides)


def apply_runtime_override(
    config: CompressionConfig,
    override: RuntimeCompressionPolicyOverride | None,
) -> tuple[CompressionConfig, dict[str, Any]]:
    """Apply a runtime override to a :class:`CompressionConfig`.

    Returns the merged config and a small metadata dict suitable for
    attaching to the resolved policy.  Only fields in
    :data:`TUNABLE_FIELDS` are forwarded; everything else (mode,
    enabled, placement, static-prefix, transforms, synthetic cache
    knobs) is left untouched.  An invalid value (non-numeric or
    out-of-range) is dropped and recorded in the metadata so
    operators see why the merge did not fire.
    """
    if override is None:
        return config, {"active": False, "applied_fields": {}}
    # Local import avoids an import cycle: policy.py loads tuning via
    # `CompressionConfig.model_rebuild()` so a top-level import here
    # would create a cycle at module load time.
    from eggpool.transcoder.compression.policy import (
        CompressionConfig as _CompressionConfig,
    )

    applied: dict[str, Any] = {}
    dropped: list[str] = []
    base_dict = config.model_dump()
    for key, raw in override.fields.items():
        if key not in TUNABLE_FIELDS:
            dropped.append(key)
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            dropped.append(key)
            continue
        if key in ("min_candidate_tokens", "min_savings_tokens"):
            base_dict[key] = int(round(value))
        else:
            base_dict[key] = float(value)
        applied[key] = base_dict[key]
    try:
        merged = _CompressionConfig.model_validate(base_dict)
    except ValidationError as exc:
        # Drop the unsafe override entirely so the request is served
        # with the previous (safe) config.  Operators see this in the
        # metadata without affecting throughput.
        return config, {
            "active": True,
            "applied_fields": {},
            "dropped_fields": dropped,
            "validation_error": str(exc),
        }
    return merged, {
        "active": True,
        "applied_fields": applied,
        "dropped_fields": dropped,
        "reason_codes": list(override.reason_codes),
    }


__all__ = [
    "CompressionTuningRecommendation",
    "REASON_APPLIED_RUNTIME_OVERRIDE",
    "REASON_BELOW_CANDIDATE_THRESHOLD_HIGH",
    "REASON_BELOW_SAVINGS_THRESHOLD_HIGH",
    "REASON_BOUNDED_BY_MAX",
    "REASON_BOUNDED_BY_MIN",
    "REASON_COOLDOWN_ACTIVE",
    "REASON_HIGH_FALLBACK_RATE",
    "REASON_HIGH_LATENCY_WARNING_RATE",
    "REASON_INSUFFICIENT_DATA",
    "REASON_LOW_POSITIVE_SAVINGS_RATE",
    "REASON_RECOMMENDATION_ONLY",
    "REASON_STRONG_SAVINGS_LOW_LATENCY",
    "RuntimeCompressionPolicyOverride",
    "TUNABLE_FIELDS",
    "TuningWindowMetrics",
    "apply_runtime_override",
    "build_runtime_override",
    "clamp_float",
    "clamp_int",
    "clamp_step",
    "compute_recommendation",
    "RuntimeCompressionPolicyOverrideRegistry",
]
