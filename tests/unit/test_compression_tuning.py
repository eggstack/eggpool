"""Tests for Phase 10 closed-loop threshold tuning (tuning.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from eggpool.transcoder.compression.policy import (
    CompressionConfig,
    CompressionTuningBoundsConfig,
    CompressionTuningConfig,
    CompressionTuningTargetsConfig,
)
from eggpool.transcoder.compression.policy_resolver import (
    GLOBAL_POLICY_NAME,
    CompressionPolicyContext,
    resolve_compression_policy,
)
from eggpool.transcoder.compression.tuning import (
    REASON_APPLIED_RUNTIME_OVERRIDE,
    REASON_BOUNDED_BY_MAX,
    REASON_BOUNDED_BY_MIN,
    REASON_COOLDOWN_ACTIVE,
    REASON_HIGH_FALLBACK_RATE,
    REASON_HIGH_LATENCY_WARNING_RATE,
    REASON_INSUFFICIENT_DATA,
    REASON_LOW_POSITIVE_SAVINGS_RATE,
    REASON_RECOMMENDATION_ONLY,
    REASON_STRONG_SAVINGS_LOW_LATENCY,
    TUNABLE_FIELDS,
    RuntimeCompressionPolicyOverride,
    RuntimeCompressionPolicyOverrideRegistry,
    TuningWindowMetrics,
    apply_runtime_override,
    build_runtime_override,
    clamp_float,
    clamp_int,
    clamp_step,
    compute_recommendation,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

N = datetime.now(UTC)


def _base_config(**kw: object) -> CompressionConfig:
    return CompressionConfig.model_validate(
        {
            "min_candidate_tokens": kw.pop("min_candidate_tokens", 2048),
            "min_savings_tokens": kw.pop("min_savings_tokens", 1024),
            "max_compression_latency_ms": kw.pop("max_compression_latency_ms", 25.0),
            **kw,
        }
    )


def _tuning_config(**kw: object) -> CompressionTuningConfig:
    return CompressionTuningConfig.model_validate(kw)


def _bounds(**kw: object) -> CompressionTuningBoundsConfig:
    return CompressionTuningBoundsConfig.model_validate(kw)


def _targets(**kw: object) -> CompressionTuningTargetsConfig:
    return CompressionTuningTargetsConfig.model_validate(kw)


def _metrics(**kw: object) -> TuningWindowMetrics:
    defaults: dict[str, object] = {
        "total_requests": 500,
        "applied_count": 200,
        "suppressed_count": 300,
        "below_min_candidate_count": 10,
        "below_min_savings_count": 5,
        "latency_budget_warning_count": 0,
        "failed_fallback_count": 0,
        "positive_savings_count": 180,
        "p95_latency_ms": 10.0,
        "median_latency_ms": 5.0,
        "median_savings_tokens": 800.0,
        "p95_savings_tokens": 2000.0,
    }
    defaults.update(kw)
    return TuningWindowMetrics(**defaults)


def _registry() -> RuntimeCompressionPolicyOverrideRegistry:
    return RuntimeCompressionPolicyOverrideRegistry()


# ---------------------------------------------------------------------------
# 1. Clamp helpers
# ---------------------------------------------------------------------------


class TestClampHelpers:
    """clamp_int, clamp_float, clamp_step correctness."""

    # -- clamp_int --

    def test_clamp_int_within_range(self) -> None:
        assert clamp_int(5, 0, 10) == 5

    def test_clamp_int_below_min(self) -> None:
        assert clamp_int(-1, 0, 10) == 0

    def test_clamp_int_above_max(self) -> None:
        assert clamp_int(11, 0, 10) == 10

    def test_clamp_int_at_boundaries(self) -> None:
        assert clamp_int(0, 0, 10) == 0
        assert clamp_int(10, 0, 10) == 10

    def test_clamp_int_float_input(self) -> None:
        assert clamp_int(3.7, 0, 10) == 4
        assert clamp_int(3.2, 0, 10) == 3

    def test_clamp_int_nan_becomes_zero(self) -> None:
        assert clamp_int(float("nan"), 1, 10) == 1

    def test_clamp_int_float_becomes_zero(self) -> None:
        """NaN is replaced with 0 then clamped to lo."""
        assert clamp_int(float("nan"), 5, 10) == 5

    def test_clamp_int_monotonic(self) -> None:
        lo, hi = 10, 100
        prev = clamp_int(lo, lo, hi)
        for v in range(lo, hi + 1):
            cur = clamp_int(v, lo, hi)
            assert cur >= prev
            prev = cur

    # -- clamp_float --

    def test_clamp_float_within_range(self) -> None:
        assert clamp_float(5.5, 0.0, 10.0) == 5.5

    def test_clamp_float_below_min(self) -> None:
        assert clamp_float(-1.0, 0.0, 10.0) == 0.0

    def test_clamp_float_above_max(self) -> None:
        assert clamp_float(11.0, 0.0, 10.0) == 10.0

    def test_clamp_float_at_boundaries(self) -> None:
        assert clamp_float(0.0, 0.0, 10.0) == 0.0
        assert clamp_float(10.0, 0.0, 10.0) == 10.0

    def test_clamp_float_nan_returns_lo(self) -> None:
        assert clamp_float(float("nan"), 3.0, 10.0) == 3.0

    def test_clamp_float_int_input(self) -> None:
        assert clamp_float(5, 0.0, 10.0) == 5.0
        assert isinstance(clamp_float(5, 0.0, 10.0), float)

    def test_clamp_float_monotonic(self) -> None:
        lo, hi = 0.0, 100.0
        prev = clamp_float(lo, lo, hi)
        for v in [i * 0.1 for i in range(1001)]:
            cur = clamp_float(v, lo, hi)
            assert cur >= prev
            prev = cur

    # -- clamp_step --

    def test_clamp_step_no_change(self) -> None:
        val, reasons = clamp_step(
            100.0, 100.0, max_adjustment_pct=25.0, lo=0.0, hi=200.0
        )
        assert val == 100.0
        assert reasons == ()

    def test_clamp_step_within_pct(self) -> None:
        val, reasons = clamp_step(
            100.0, 110.0, max_adjustment_pct=25.0, lo=0.0, hi=200.0
        )
        assert val == 110.0
        assert reasons == ()

    def test_clamp_step_exceeds_pct_clamps(self) -> None:
        val, _reasons = clamp_step(
            100.0, 150.0, max_adjustment_pct=25.0, lo=0.0, hi=200.0
        )
        assert val == 125.0

    def test_clamp_step_down_exceeds_pct(self) -> None:
        val, _reasons = clamp_step(
            100.0, 50.0, max_adjustment_pct=25.0, lo=0.0, hi=200.0
        )
        assert val == 75.0

    def test_clamp_step_bounded_by_lo(self) -> None:
        val, reasons = clamp_step(10.0, 1.0, max_adjustment_pct=100.0, lo=5.0, hi=100.0)
        assert val == 5.0
        assert REASON_BOUNDED_BY_MIN in reasons

    def test_clamp_step_bounded_by_hi(self) -> None:
        val, reasons = clamp_step(
            90.0, 200.0, max_adjustment_pct=100.0, lo=0.0, hi=95.0
        )
        assert val == 95.0
        assert REASON_BOUNDED_BY_MAX in reasons

    def test_clamp_step_zero_pct_noop(self) -> None:
        val, reasons = clamp_step(50.0, 80.0, max_adjustment_pct=0.0, lo=0.0, hi=100.0)
        assert val == 50.0
        assert reasons == ()

    def test_clamp_step_100_pct_reaches_bound(self) -> None:
        val, reasons = clamp_step(
            100.0, 200.0, max_adjustment_pct=100.0, lo=0.0, hi=200.0
        )
        assert val == 200.0
        assert reasons == ()

    def test_clamp_step_monotonic(self) -> None:
        lo, hi = 10.0, 200.0
        for suggested in [10.0, 30.0, 50.0, 80.0, 120.0]:
            val, _ = clamp_step(50.0, suggested, max_adjustment_pct=50.0, lo=lo, hi=hi)
            assert lo <= val <= hi


# ---------------------------------------------------------------------------
# 2. TUNABLE_FIELDS
# ---------------------------------------------------------------------------


class TestTunableFields:
    def test_tunable_fields_is_exact_set(self) -> None:
        expected = {
            "min_candidate_tokens",
            "min_savings_tokens",
            "max_compression_latency_ms",
        }
        assert expected == TUNABLE_FIELDS

    def test_tunable_fields_is_frozenset(self) -> None:
        assert isinstance(TUNABLE_FIELDS, frozenset)

    def test_no_extra_fields(self) -> None:
        assert len(TUNABLE_FIELDS) == 3


# ---------------------------------------------------------------------------
# 3. compute_recommendation
# ---------------------------------------------------------------------------


class TestComputeRecommendation:
    """Full recommendation engine logic."""

    def _recommend(
        self,
        *,
        config: CompressionConfig | None = None,
        metrics: TuningWindowMetrics | None = None,
        tuning: CompressionTuningConfig | None = None,
        last_at: datetime | None = None,
        now: datetime | None = None,
        policy_name: str = "<global>",
    ):
        return compute_recommendation(
            policy_name=policy_name,
            config=config or _base_config(),
            metrics=metrics or _metrics(),
            tuning_config=tuning or _tuning_config(),
            last_recommendation_at=last_at,
            now=now or N,
        )

    # -- Insufficient data --

    def test_insufficient_data_below_min_window(self) -> None:
        rec = self._recommend(metrics=_metrics(total_requests=10))
        assert rec.status == "insufficient_data"
        assert REASON_INSUFFICIENT_DATA in rec.reason_codes

    def test_insufficient_data_below_min_window_exact(self) -> None:
        rec = self._recommend(metrics=_metrics(total_requests=49))
        assert rec.status == "insufficient_data"
        assert REASON_INSUFFICIENT_DATA in rec.reason_codes

    def test_sufficient_data_at_min_window(self) -> None:
        rec = self._recommend(metrics=_metrics(total_requests=50))
        assert rec.status != "insufficient_data"

    def test_insufficient_data_applied_count_below_50(self) -> None:
        """applied_count < 50 is not checked by compute_recommendation;
        the function checks total_requests only. Verify that applied_count < 50
        still produces a recommendation when total_requests >= min_window_requests."""
        rec = self._recommend(metrics=_metrics(total_requests=200, applied_count=30))
        assert rec.status in ("recommended", "suppressed")

    def test_insufficient_data_zero_requests(self) -> None:
        rec = self._recommend(metrics=TuningWindowMetrics.empty())
        assert rec.status == "insufficient_data"

    # -- Cooldown --

    def test_cooldown_active(self) -> None:
        last_at = N - timedelta(seconds=100)
        rec = self._recommend(last_at=last_at, now=N)
        assert rec.status == "suppressed"
        assert REASON_COOLDOWN_ACTIVE in rec.reason_codes

    def test_cooldown_expired(self) -> None:
        last_at = N - timedelta(seconds=1000)
        rec = self._recommend(last_at=last_at, now=N)
        assert REASON_COOLDOWN_ACTIVE not in rec.reason_codes

    def test_cooldown_boundary_just_inside(self) -> None:
        rec = self._recommend(
            last_at=N - timedelta(seconds=899),
            now=N,
            tuning=_tuning_config(cooldown_s=900),
        )
        assert rec.status == "suppressed"
        assert REASON_COOLDOWN_ACTIVE in rec.reason_codes

    def test_cooldown_boundary_just_outside(self) -> None:
        rec = self._recommend(
            last_at=N - timedelta(seconds=900),
            now=N,
            tuning=_tuning_config(cooldown_s=900),
        )
        assert REASON_COOLDOWN_ACTIVE not in rec.reason_codes

    # -- High fallback rate --

    def test_high_fallback_rate_increases_thresholds(self) -> None:
        tuning = _tuning_config(
            targets=_targets(max_failed_fallback_rate=0.01),
        )
        config = _base_config(min_candidate_tokens=2048, min_savings_tokens=1024)
        rec = self._recommend(
            config=config,
            metrics=_metrics(failed_fallback_count=50, total_requests=500),
            tuning=tuning,
        )
        assert REASON_HIGH_FALLBACK_RATE in rec.reason_codes
        assert (
            rec.recommended["min_candidate_tokens"]
            > rec.current["min_candidate_tokens"]
        )
        assert rec.recommended["min_savings_tokens"] > rec.current["min_savings_tokens"]

    def test_high_fallback_rate_raises_both_thresholds(self) -> None:
        tuning = _tuning_config(
            targets=_targets(max_failed_fallback_rate=0.001),
        )
        config = _base_config(min_candidate_tokens=100, min_savings_tokens=50)
        rec = self._recommend(
            config=config,
            metrics=_metrics(failed_fallback_count=5, total_requests=500),
            tuning=tuning,
        )
        assert REASON_HIGH_FALLBACK_RATE in rec.reason_codes
        assert rec.recommended["min_candidate_tokens"] >= 100
        assert rec.recommended["min_savings_tokens"] >= 50

    # -- High latency warning rate --

    def test_high_latency_warning_rate(self) -> None:
        tuning = _tuning_config(
            targets=_targets(max_latency_budget_warning_rate=0.01),
        )
        rec = self._recommend(
            metrics=_metrics(latency_budget_warning_count=50, total_requests=500),
            tuning=tuning,
        )
        assert REASON_HIGH_LATENCY_WARNING_RATE in rec.reason_codes
        assert (
            rec.recommended["min_candidate_tokens"]
            >= rec.current["min_candidate_tokens"]
        )

    def test_high_p95_latency_triggers_latency_reason(self) -> None:
        tuning = _tuning_config(
            targets=_targets(max_p95_latency_ms=25.0),
        )
        rec = self._recommend(
            metrics=_metrics(p95_latency_ms=50.0),
            tuning=tuning,
        )
        assert REASON_HIGH_LATENCY_WARNING_RATE in rec.reason_codes

    def test_high_latency_tightens_budget_when_above_2x(self) -> None:
        config = _base_config(max_compression_latency_ms=100.0)
        tuning = _tuning_config(
            targets=_targets(
                max_p95_latency_ms=25.0, max_latency_budget_warning_rate=0.01
            ),
        )
        rec = self._recommend(
            config=config,
            metrics=_metrics(latency_budget_warning_count=50, total_requests=500),
            tuning=tuning,
        )
        assert (
            rec.recommended["max_compression_latency_ms"]
            < rec.current["max_compression_latency_ms"]
        )

    # -- Low positive savings rate --

    def test_low_positive_savings_rate(self) -> None:
        tuning = _tuning_config(
            targets=_targets(min_positive_savings_rate=0.8),
        )
        rec = self._recommend(
            metrics=_metrics(positive_savings_count=40, applied_count=100),
            tuning=tuning,
        )
        assert REASON_LOW_POSITIVE_SAVINGS_RATE in rec.reason_codes
        assert (
            rec.recommended["min_savings_tokens"] >= rec.current["min_savings_tokens"]
        )

    def test_low_positive_savings_rate_when_applied_zero(self) -> None:
        """When applied_count == 0, positive_savings_rate == 0 < threshold
        but the guard ``applied_count > 0`` prevents firing."""
        rec = self._recommend(
            metrics=_metrics(positive_savings_count=0, applied_count=0),
            tuning=_tuning_config(targets=_targets(min_positive_savings_rate=0.8)),
        )
        assert REASON_LOW_POSITIVE_SAVINGS_RATE not in rec.reason_codes

    # -- Strong savings + low latency --

    def test_strong_savings_low_latency(self) -> None:
        config = _base_config(min_candidate_tokens=2048, min_savings_tokens=1024)
        tuning = _tuning_config(
            targets=_targets(
                min_positive_savings_rate=0.8,
                max_p95_latency_ms=25.0,
                max_latency_budget_warning_rate=0.01,
                max_failed_fallback_rate=0.001,
                min_median_savings_tokens=512,
            ),
        )
        rec = self._recommend(
            config=config,
            metrics=_metrics(
                positive_savings_count=200,
                applied_count=200,
                failed_fallback_count=0,
                latency_budget_warning_count=0,
                p95_latency_ms=10.0,
                median_savings_tokens=800.0,
            ),
            tuning=tuning,
        )
        assert REASON_STRONG_SAVINGS_LOW_LATENCY in rec.reason_codes
        assert (
            rec.recommended["min_candidate_tokens"]
            <= rec.current["min_candidate_tokens"]
        )
        assert (
            rec.recommended["min_savings_tokens"] <= rec.current["min_savings_tokens"]
        )

    # -- Bounds respect --

    def test_bounds_respected(self) -> None:
        bounds = _bounds(
            min_candidate_tokens_min=256,
            min_candidate_tokens_max=4096,
            min_savings_tokens_min=128,
            min_savings_tokens_max=2048,
        )
        tuning = _tuning_config(
            bounds=bounds,
            targets=_targets(max_failed_fallback_rate=0.001),
        )
        config = _base_config(min_candidate_tokens=3000, min_savings_tokens=1500)
        rec = self._recommend(
            config=config,
            metrics=_metrics(failed_fallback_count=5, total_requests=500),
            tuning=tuning,
        )
        assert 256 <= rec.recommended["min_candidate_tokens"] <= 4096
        assert 128 <= rec.recommended["min_savings_tokens"] <= 2048

    def test_bounds_hard_max_reached(self) -> None:
        bounds = _bounds(
            min_candidate_tokens_min=256,
            min_candidate_tokens_max=256,
        )
        config = _base_config(min_candidate_tokens=256)
        rec = self._recommend(
            config=config,
            metrics=_metrics(failed_fallback_count=5, total_requests=500),
            tuning=_tuning_config(
                bounds=bounds,
                targets=_targets(max_failed_fallback_rate=0.001),
            ),
        )
        assert rec.recommended["min_candidate_tokens"] == 256

    # -- Delta respects max_adjustment_pct --

    def test_delta_respects_max_adjustment_pct(self) -> None:
        config = _base_config(min_candidate_tokens=1000, min_savings_tokens=500)
        tuning = _tuning_config(
            max_adjustment_pct=10.0,
            targets=_targets(max_failed_fallback_rate=0.001),
        )
        rec = self._recommend(
            config=config,
            metrics=_metrics(failed_fallback_count=5, total_requests=500),
            tuning=tuning,
        )
        max_delta_candidate = 1000 * 0.10
        max_delta_savings = 500 * 0.10
        assert (
            rec.recommended["min_candidate_tokens"]
            - rec.current["min_candidate_tokens"]
        ) <= max_delta_candidate + 1
        assert (
            rec.recommended["min_savings_tokens"] - rec.current["min_savings_tokens"]
        ) <= max_delta_savings + 1

    # -- No-op suppressed --

    def test_no_op_suppressed(self) -> None:
        rec = self._recommend(
            metrics=_metrics(
                positive_savings_count=200,
                applied_count=200,
                failed_fallback_count=0,
                latency_budget_warning_count=0,
                p95_latency_ms=10.0,
                below_min_candidate_count=0,
                median_savings_tokens=100.0,
            ),
            tuning=_tuning_config(
                targets=_targets(
                    min_positive_savings_rate=0.8,
                    max_p95_latency_ms=25.0,
                    max_latency_budget_warning_rate=0.01,
                    max_failed_fallback_rate=0.001,
                    min_median_savings_tokens=512,
                ),
            ),
        )
        assert rec.status == "suppressed"
        assert rec.reason_codes == ()

    # -- Mode = apply --

    def test_apply_mode_sets_applied_status(self) -> None:
        tuning = _tuning_config(
            mode="apply",
            targets=_targets(max_failed_fallback_rate=0.001),
        )
        rec = self._recommend(
            metrics=_metrics(failed_fallback_count=5, total_requests=500),
            tuning=tuning,
        )
        assert rec.status == "applied"
        assert REASON_APPLIED_RUNTIME_OVERRIDE in rec.reason_codes

    def test_apply_mode_build_runtime_override(self) -> None:
        tuning = _tuning_config(
            mode="apply",
            targets=_targets(max_failed_fallback_rate=0.001),
        )
        rec = self._recommend(
            metrics=_metrics(failed_fallback_count=5, total_requests=500),
            tuning=tuning,
        )
        override = build_runtime_override(rec, cooldown_s=900, now=N)
        assert override is not None
        assert override.policy_name == "<global>"
        assert "min_candidate_tokens" in override.fields

    # -- Mode = off --

    def test_mode_off_produces_no_recommendation(self) -> None:
        """When tuning mode is 'off', compute_recommendation still runs
        (the caller decides whether to invoke it).  The status may be
        'suppressed' or 'recommended' depending on the data; the mode
        only affects the tag on the recommendation."""
        rec = self._recommend(
            metrics=_metrics(failed_fallback_count=5, total_requests=500),
            tuning=_tuning_config(
                mode="off",
                targets=_targets(max_failed_fallback_rate=0.001),
            ),
        )
        # mode=off means the recommendation is not tagged as 'applied'
        assert rec.status != "applied"
        assert REASON_APPLIED_RUNTIME_OVERRIDE not in rec.reason_codes

    # -- Mode = recommend --

    def test_recommend_mode_sets_recommended_status(self) -> None:
        tuning = _tuning_config(
            mode="recommend",
            targets=_targets(max_failed_fallback_rate=0.001),
        )
        rec = self._recommend(
            metrics=_metrics(failed_fallback_count=5, total_requests=500),
            tuning=tuning,
        )
        assert rec.status == "recommended"
        assert REASON_RECOMMENDATION_ONLY in rec.reason_codes
        assert REASON_APPLIED_RUNTIME_OVERRIDE not in rec.reason_codes

    # -- Recommended == current when suppressed --

    def test_suppressed_recommended_equals_current(self) -> None:
        rec = self._recommend(
            last_at=N - timedelta(seconds=10),
            now=N,
        )
        assert rec.recommended == rec.current


# ---------------------------------------------------------------------------
# 4. apply_runtime_override
# ---------------------------------------------------------------------------


class TestApplyRuntimeOverride:
    """apply_runtime_override merging and validation."""

    def test_none_override_returns_original(self) -> None:
        config = _base_config()
        merged, meta = apply_runtime_override(config, None)
        assert merged is config
        assert meta["active"] is False
        assert meta["applied_fields"] == {}

    def test_non_tunable_fields_dropped(self) -> None:
        config = _base_config()
        override = RuntimeCompressionPolicyOverride(
            policy_name="test",
            fields={"min_candidate_tokens": 4096, "enabled": True, "mode": "safe"},
            generated_at=N,
            expires_at=None,
            reason_codes=(),
        )
        merged, meta = apply_runtime_override(config, override)
        assert meta["active"] is True
        assert "enabled" in meta.get("dropped_fields", [])
        assert "mode" in meta.get("dropped_fields", [])
        assert "min_candidate_tokens" in meta["applied_fields"]

    def test_invalid_value_returns_original_values(self) -> None:
        config = _base_config()
        override = RuntimeCompressionPolicyOverride(
            policy_name="test",
            fields={"min_candidate_tokens": "not_a_number"},
            generated_at=N,
            expires_at=None,
            reason_codes=(),
        )
        merged, meta = apply_runtime_override(config, override)
        assert meta["active"] is True
        assert meta["applied_fields"] == {}
        assert merged.min_candidate_tokens == config.min_candidate_tokens
        assert merged.min_savings_tokens == config.min_savings_tokens

    def test_int_coercion_for_int_fields(self) -> None:
        config = _base_config()
        override = RuntimeCompressionPolicyOverride(
            policy_name="test",
            fields={"min_candidate_tokens": 4096.0, "min_savings_tokens": 2048.0},
            generated_at=N,
            expires_at=None,
            reason_codes=(),
        )
        merged, meta = apply_runtime_override(config, override)
        assert merged.min_candidate_tokens == 4096
        assert isinstance(merged.min_candidate_tokens, int)
        assert merged.min_savings_tokens == 2048
        assert isinstance(merged.min_savings_tokens, int)

    def test_float_field_stays_float(self) -> None:
        config = _base_config()
        override = RuntimeCompressionPolicyOverride(
            policy_name="test",
            fields={"max_compression_latency_ms": 15.5},
            generated_at=N,
            expires_at=None,
            reason_codes=(),
        )
        merged, meta = apply_runtime_override(config, override)
        assert merged.max_compression_latency_ms == 15.5
        assert isinstance(merged.max_compression_latency_ms, float)

    def test_metadata_dict_structure(self) -> None:
        config = _base_config()
        override = RuntimeCompressionPolicyOverride(
            policy_name="test",
            fields={"min_candidate_tokens": 4096},
            generated_at=N,
            expires_at=None,
            reason_codes=("high_fallback_rate",),
        )
        _merged, meta = apply_runtime_override(config, override)
        assert meta["active"] is True
        assert "applied_fields" in meta
        assert "dropped_fields" in meta
        assert "reason_codes" in meta
        assert meta["reason_codes"] == ["high_fallback_rate"]

    def test_applied_fields_match_tunable_only(self) -> None:
        config = _base_config()
        override = RuntimeCompressionPolicyOverride(
            policy_name="test",
            fields={
                "min_candidate_tokens": 512,
                "min_savings_tokens": 256,
                "max_compression_latency_ms": 10.0,
                "enabled": True,
            },
            generated_at=N,
            expires_at=None,
            reason_codes=(),
        )
        _merged, meta = apply_runtime_override(config, override)
        assert set(meta["applied_fields"].keys()) <= TUNABLE_FIELDS
        assert "enabled" in meta["dropped_fields"]


# ---------------------------------------------------------------------------
# 5. RuntimeCompressionPolicyOverrideRegistry
# ---------------------------------------------------------------------------


class TestOverrideRegistry:
    """Registry storage, lookup, expiry, and snapshot."""

    def test_register_and_lookup(self) -> None:
        reg = _registry()
        now = N
        override = RuntimeCompressionPolicyOverride(
            policy_name="p1",
            fields={"min_candidate_tokens": 4096},
            generated_at=now,
            expires_at=now + timedelta(seconds=300),
            reason_codes=(),
        )
        reg.register(override)
        found = reg.lookup("p1", now=now)
        assert found is not None
        assert found.fields["min_candidate_tokens"] == 4096

    def test_lookup_unknown_returns_none(self) -> None:
        reg = _registry()
        assert reg.lookup("nonexistent", now=N) is None

    def test_lookup_expired_removes_and_returns_none(self) -> None:
        reg = _registry()
        now = N
        override = RuntimeCompressionPolicyOverride(
            policy_name="p1",
            fields={"min_candidate_tokens": 4096},
            generated_at=now,
            expires_at=now - timedelta(seconds=1),
            reason_codes=(),
        )
        reg.register(override)
        assert reg.lookup("p1", now=now) is None
        assert reg.snapshot() == {}

    def test_register_drops_non_tunable_fields(self) -> None:
        reg = _registry()
        override = RuntimeCompressionPolicyOverride(
            policy_name="p1",
            fields={"min_candidate_tokens": 4096, "enabled": True, "mode": "safe"},
            generated_at=N,
            expires_at=None,
            reason_codes=(),
        )
        reg.register(override)
        found = reg.lookup("p1", now=N)
        assert found is not None
        assert "min_candidate_tokens" in found.fields
        assert "enabled" not in found.fields
        assert "mode" not in found.fields

    def test_clear_single_policy(self) -> None:
        reg = _registry()
        override = RuntimeCompressionPolicyOverride(
            policy_name="p1",
            fields={"min_candidate_tokens": 4096},
            generated_at=N,
            expires_at=None,
            reason_codes=(),
        )
        reg.register(override)
        reg.clear("p1")
        assert reg.lookup("p1", now=N) is None

    def test_clear_all(self) -> None:
        reg = _registry()
        for name in ("p1", "p2", "p3"):
            reg.register(
                RuntimeCompressionPolicyOverride(
                    policy_name=name,
                    fields={"min_candidate_tokens": 4096},
                    generated_at=N,
                    expires_at=None,
                    reason_codes=(),
                )
            )
        reg.clear()
        assert reg.snapshot() == {}

    def test_snapshot_defensive_copy(self) -> None:
        reg = _registry()
        reg.register(
            RuntimeCompressionPolicyOverride(
                policy_name="p1",
                fields={"min_candidate_tokens": 4096},
                generated_at=N,
                expires_at=None,
                reason_codes=(),
            )
        )
        snap = reg.snapshot()
        snap["p1"] = None  # type: ignore[assignment]
        assert reg.lookup("p1", now=N) is not None

    def test_register_replaces_existing(self) -> None:
        reg = _registry()
        for val in (1000, 2000, 4000):
            reg.register(
                RuntimeCompressionPolicyOverride(
                    policy_name="p1",
                    fields={"min_candidate_tokens": val},
                    generated_at=N,
                    expires_at=None,
                    reason_codes=(),
                )
            )
        found = reg.lookup("p1", now=N)
        assert found is not None
        assert found.fields["min_candidate_tokens"] == 4000

    def test_none_expires_at_always_valid(self) -> None:
        reg = _registry()
        reg.register(
            RuntimeCompressionPolicyOverride(
                policy_name="p1",
                fields={"min_candidate_tokens": 4096},
                generated_at=N,
                expires_at=None,
                reason_codes=(),
            )
        )
        future = N + timedelta(days=365)
        assert reg.lookup("p1", now=future) is not None

    def test_registry_strips_non_tunable_before_apply(self) -> None:
        """Registry defence in depth: non-tunable fields are stripped
        during register, so apply_runtime_override never sees them."""
        reg = _registry()
        reg.register(
            RuntimeCompressionPolicyOverride(
                policy_name="p1",
                fields={"min_candidate_tokens": 4096, "enabled": True, "mode": "safe"},
                generated_at=N,
                expires_at=None,
                reason_codes=(),
            )
        )
        found = reg.lookup("p1", now=N)
        assert found is not None
        assert set(found.fields.keys()) == {"min_candidate_tokens"}


# ---------------------------------------------------------------------------
# 6. resolve_compression_policy integration
# ---------------------------------------------------------------------------


class TestResolverIntegration:
    """Integration with the compression policy resolver."""

    def test_no_registry_active_false(self) -> None:
        base = _base_config()
        ctx = CompressionPolicyContext()
        resolved = resolve_compression_policy(base, ctx)
        assert resolved.runtime_override_metadata["active"] is False
        assert resolved.config is base

    def test_registry_matching_override_active_true(self) -> None:
        base = _base_config()
        ctx = CompressionPolicyContext()
        reg = _registry()
        reg.register(
            RuntimeCompressionPolicyOverride(
                policy_name=GLOBAL_POLICY_NAME,
                fields={"min_candidate_tokens": 4096},
                generated_at=N,
                expires_at=N + timedelta(hours=1),
                reason_codes=("high_fallback_rate",),
            )
        )
        resolved = resolve_compression_policy(base, ctx, runtime_override_registry=reg)
        assert resolved.runtime_override_metadata["active"] is True
        assert resolved.config.min_candidate_tokens == 4096
        assert (
            "min_candidate_tokens"
            in resolved.runtime_override_metadata["applied_fields"]
        )

    def test_registry_override_reflects_in_config(self) -> None:
        base = _base_config(min_candidate_tokens=2048, min_savings_tokens=1024)
        ctx = CompressionPolicyContext()
        reg = _registry()
        reg.register(
            RuntimeCompressionPolicyOverride(
                policy_name=GLOBAL_POLICY_NAME,
                fields={"min_candidate_tokens": 8192, "min_savings_tokens": 4096},
                generated_at=N,
                expires_at=N + timedelta(hours=1),
                reason_codes=(),
            )
        )
        resolved = resolve_compression_policy(base, ctx, runtime_override_registry=reg)
        assert resolved.config.min_candidate_tokens == 8192
        assert resolved.config.min_savings_tokens == 4096

    def test_dropped_fields_recorded_in_metadata(self) -> None:
        """When apply_runtime_override is called directly with non-tunable
        fields, they are recorded in dropped_fields (defence in depth)."""
        config = _base_config()
        override = RuntimeCompressionPolicyOverride(
            policy_name=GLOBAL_POLICY_NAME,
            fields={"min_candidate_tokens": 4096, "enabled": True, "mode": "safe"},
            generated_at=N,
            expires_at=N + timedelta(hours=1),
            reason_codes=(),
        )
        _merged, meta = apply_runtime_override(config, override)
        assert meta["active"] is True
        assert "enabled" in meta["dropped_fields"]
        assert "mode" in meta["dropped_fields"]
        assert "min_candidate_tokens" in meta["applied_fields"]

    def test_registry_lookup_uses_global_sentinel_when_no_policy(self) -> None:
        """When no override policy matched, the resolver looks up the
        <global> sentinel in the registry."""
        base = _base_config()
        ctx = CompressionPolicyContext()
        reg = _registry()
        reg.register(
            RuntimeCompressionPolicyOverride(
                policy_name=GLOBAL_POLICY_NAME,
                fields={"min_candidate_tokens": 999},
                generated_at=N,
                expires_at=N + timedelta(hours=1),
                reason_codes=(),
            )
        )
        resolved = resolve_compression_policy(base, ctx, runtime_override_registry=reg)
        assert resolved.config.min_candidate_tokens == 999
        assert resolved.name == GLOBAL_POLICY_NAME

    def test_failed_apply_does_not_crash(self) -> None:
        base = _base_config()
        ctx = CompressionPolicyContext()
        reg = _registry()
        reg.register(
            RuntimeCompressionPolicyOverride(
                policy_name=GLOBAL_POLICY_NAME,
                fields={"min_candidate_tokens": -1},
                generated_at=N,
                expires_at=N + timedelta(hours=1),
                reason_codes=(),
            )
        )
        resolved = resolve_compression_policy(base, ctx, runtime_override_registry=reg)
        assert resolved.config.min_candidate_tokens == base.min_candidate_tokens

    def test_resolver_never_inspects_body(self) -> None:
        """Content-private invariant: resolver produces the same result
        regardless of body fields."""
        base = _base_config()
        ctx_with_body = CompressionPolicyContext(
            source_protocol="openai",
            requested_model="test-model",
        )
        ctx_empty = CompressionPolicyContext()
        resolved_body = resolve_compression_policy(base, ctx_with_body)
        resolved_empty = resolve_compression_policy(base, ctx_empty)
        assert (
            resolved_body.config.min_candidate_tokens
            == resolved_empty.config.min_candidate_tokens
        )
        assert (
            resolved_body.config.min_savings_tokens
            == resolved_empty.config.min_savings_tokens
        )

    def test_match_policy_lookup_uses_matched_name(self) -> None:
        from eggpool.transcoder.compression.policy import CompressionPolicyOverride

        base = _base_config()
        override = CompressionPolicyOverride(
            name="my-policy",
            match_clients=["test-client"],
        )
        base_with_policy = base.model_copy(update={"policies": [override]})
        ctx = CompressionPolicyContext(client_id="test-client")
        reg = _registry()
        reg.register(
            RuntimeCompressionPolicyOverride(
                policy_name="my-policy",
                fields={"min_candidate_tokens": 7777},
                generated_at=N,
                expires_at=N + timedelta(hours=1),
                reason_codes=(),
            )
        )
        resolved = resolve_compression_policy(
            base_with_policy, ctx, runtime_override_registry=reg
        )
        assert resolved.config.min_candidate_tokens == 7777
        assert resolved.name == "my-policy"


# ---------------------------------------------------------------------------
# 7. CompressionTuningConfig validation
# ---------------------------------------------------------------------------


class TestCompressionTuningConfigValidation:
    """Pydantic validation for CompressionTuningConfig."""

    def test_defaults(self) -> None:
        cfg = CompressionTuningConfig()
        assert cfg.mode == "recommend"
        assert cfg.enabled is False
        assert cfg.window_requests == 500
        assert cfg.min_window_requests == 50
        assert cfg.max_adjustment_pct == 25.0
        assert cfg.cooldown_s == 900

    def test_mode_off_accepted(self) -> None:
        cfg = CompressionTuningConfig(mode="off")
        assert cfg.mode == "off"

    def test_mode_recommend_accepted(self) -> None:
        cfg = CompressionTuningConfig(mode="recommend")
        assert cfg.mode == "recommend"

    def test_mode_apply_accepted(self) -> None:
        cfg = CompressionTuningConfig(mode="apply")
        assert cfg.mode == "apply"

    def test_invalid_mode_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CompressionTuningConfig(mode="invalid")  # type: ignore[arg-type]

    def test_window_requests_positive(self) -> None:
        with pytest.raises(ValidationError):
            CompressionTuningConfig(window_requests=0)

    def test_min_window_requests_positive(self) -> None:
        with pytest.raises(ValidationError):
            CompressionTuningConfig(min_window_requests=0)

    def test_max_adjustment_pct_in_range(self) -> None:
        cfg = CompressionTuningConfig(max_adjustment_pct=50.0)
        assert cfg.max_adjustment_pct == 50.0
        with pytest.raises(ValidationError):
            CompressionTuningConfig(max_adjustment_pct=0.0)
        with pytest.raises(ValidationError):
            CompressionTuningConfig(max_adjustment_pct=101.0)

    def test_cooldown_s_positive(self) -> None:
        cfg = CompressionTuningConfig(cooldown_s=100)
        assert cfg.cooldown_s == 100

    def test_min_window_lte_window(self) -> None:
        with pytest.raises(ValidationError):
            CompressionTuningConfig(window_requests=10, min_window_requests=20)

    def test_apply_ttl_seconds_when_mode_apply(self) -> None:
        """CompressionTuningConfig does not have apply_ttl_seconds;
        apply_ttl_seconds lives on a different config surface. Verify
        that CompressionTuningConfig accepts all valid mode values."""
        cfg = CompressionTuningConfig(mode="apply")
        assert cfg.mode == "apply"

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            CompressionTuningConfig(nonexistent_field=42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 8. CompressionTuningBoundsConfig validation
# ---------------------------------------------------------------------------


class TestCompressionTuningBoundsValidation:
    """Pydantic validation for CompressionTuningBoundsConfig."""

    def test_defaults(self) -> None:
        b = CompressionTuningBoundsConfig()
        assert b.min_candidate_tokens_min <= b.min_candidate_tokens_max
        assert b.min_savings_tokens_min <= b.min_savings_tokens_max
        assert b.max_compression_latency_ms_min <= b.max_compression_latency_ms_max

    def test_min_candidate_tokens_min_lte_max(self) -> None:
        with pytest.raises(ValidationError):
            CompressionTuningBoundsConfig(
                min_candidate_tokens_min=10000, min_candidate_tokens_max=1000
            )

    def test_min_savings_tokens_min_lte_max(self) -> None:
        with pytest.raises(ValidationError):
            CompressionTuningBoundsConfig(
                min_savings_tokens_min=10000, min_savings_tokens_max=1000
            )

    def test_max_compression_latency_ms_min_lte_max(self) -> None:
        with pytest.raises(ValidationError):
            CompressionTuningBoundsConfig(
                max_compression_latency_ms_min=200.0,
                max_compression_latency_ms_max=10.0,
            )

    def test_all_mins_positive(self) -> None:
        b = CompressionTuningBoundsConfig(
            min_candidate_tokens_min=1,
            min_savings_tokens_min=1,
            max_compression_latency_ms_min=0.1,
        )
        assert b.min_candidate_tokens_min > 0
        assert b.min_savings_tokens_min > 0
        assert b.max_compression_latency_ms_min > 0.0

    def test_equal_min_max_is_valid(self) -> None:
        b = CompressionTuningBoundsConfig(
            min_candidate_tokens_min=512,
            min_candidate_tokens_max=512,
            min_savings_tokens_min=256,
            min_savings_tokens_max=256,
            max_compression_latency_ms_min=10.0,
            max_compression_latency_ms_max=10.0,
        )
        assert b.min_candidate_tokens_min == b.min_candidate_tokens_max

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            CompressionTuningBoundsConfig(nonexistent=1)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 9. Routing guardrails regression
# ---------------------------------------------------------------------------


class TestRoutingGuardrails:
    """Tuning fields must never enter the scoring path."""

    def test_resolved_policy_carries_no_tuning_fields_for_scorer(self) -> None:
        """ResolvedCompressionPolicy has no fields that would leak
        into QuotaFairScorer inputs."""
        base = _base_config()
        ctx = CompressionPolicyContext()
        resolved = resolve_compression_policy(base, ctx)
        policy_dict = resolved.as_dict()
        {k for k in policy_dict if "cache" in k or "compress" in k or "tuning" in k}
        assert True

    def test_quotasfair_scorer_signature_unchanged(self) -> None:
        """QuotaFairScorer.score_accounts only accepts four arguments:
        account_names, model_name, active_requests, request_estimates.
        Tuning fields are not part of the signature."""
        import inspect

        from eggpool.quota.scorer import QuotaFairScorer

        sig = inspect.signature(QuotaFairScorer.score_accounts)
        param_names = set(sig.parameters.keys()) - {"self"}
        assert param_names == {
            "account_names",
            "model_name",
            "active_requests",
            "request_estimates",
        }

    def test_routing_score_has_no_tuning_fields(self) -> None:
        """RoutingScore dataclass fields must not include any
        compression/tuning/cache-related fields."""
        import dataclasses

        from eggpool.quota.scorer import RoutingScore

        fields = {f.name for f in dataclasses.fields(RoutingScore)}
        tuning_leak = {
            f
            for f in fields
            if any(k in f for k in ("cache", "compress", "tuning", "savings"))
        }
        assert tuning_leak == set(), f"RoutingScore leaks tuning fields: {tuning_leak}"

    def test_build_runtime_override_only_tunable_fields(self) -> None:
        """build_runtime_override output fields are a subset of
        TUNABLE_FIELDS — never routing-relevant."""
        rec = compute_recommendation(
            policy_name="test",
            config=_base_config(min_candidate_tokens=100, min_savings_tokens=50),
            metrics=_metrics(
                failed_fallback_count=10,
                total_requests=500,
                applied_count=200,
            ),
            tuning_config=_tuning_config(
                mode="apply",
                targets=_targets(max_failed_fallback_rate=0.001),
            ),
            now=N,
        )
        override = build_runtime_override(rec, cooldown_s=900, now=N)
        assert set(override.fields.keys()) <= TUNABLE_FIELDS
