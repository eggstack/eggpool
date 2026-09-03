"""Contracts for bounded semantic model-router observability."""

from __future__ import annotations

from eggpool.metrics.model_router import ModelRouterMetrics


def test_metrics_record_structural_decisions_without_session_data() -> None:
    metrics = ModelRouterMetrics()
    metrics.record_resolution(
        virtual_model="implementer",
        concrete_model="model-fast/provider-a",
        source="selector",
        affinity_hit=False,
        selector_attempts=2,
        fallback_reason=None,
        repair_attempted=True,
        repair_succeeded=True,
        latency_ms=3.5,
    )
    metrics.record_resolution(
        virtual_model="implementer",
        concrete_model="model-fast/provider-a",
        source="selector",
        affinity_hit=True,
        selector_attempts=0,
        fallback_reason=None,
        repair_attempted=False,
        repair_succeeded=False,
        latency_ms=0.2,
    )

    snapshot = metrics.snapshot()
    assert snapshot["virtual_requests"] == 2
    assert snapshot["selector_decisions"] == {"selector": 2}
    assert snapshot["affinity"] == {"hits": 1, "misses": 1}
    assert snapshot["repair"] == {"attempts": 1, "successes": 1}
    assert snapshot["selections"] == {
        "implementer|model-fast/provider-a": 2,
    }
    assert "session" not in repr(snapshot)


def test_metrics_bound_fallback_labels_and_selection_cardinality() -> None:
    metrics = ModelRouterMetrics()
    for index in range(300):
        metrics.record_resolution(
            virtual_model=f"virtual-{index}",
            concrete_model="default",
            source="default",
            affinity_hit=False,
            selector_attempts=1,
            fallback_reason="invalid_output",
            repair_attempted=False,
            repair_succeeded=False,
            latency_ms=-1,
        )

    snapshot = metrics.snapshot()
    assert snapshot["fallbacks"]["invalid_output"] == 300
    assert len(snapshot["selections"]) <= 257
    assert snapshot["resolution_latency_ms"]["max"] == 0.0
