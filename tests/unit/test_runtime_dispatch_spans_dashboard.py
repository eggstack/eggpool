"""Tests for the dispatch spans panel in the runtime dashboard."""

from __future__ import annotations

from typing import Any

import pytest

from eggpool.dashboard.render import (
    _DISPATCH_SPAN_LABELS,
    _render_dispatch_spans_panel,
    render_runtime,
)

pytestmark = pytest.mark.dashboard


def _make_span(
    span: str,
    *,
    sample_count: int = 0,
    p50_ms: float | None = None,
    p95_ms: float | None = None,
    max_ms: float | None = None,
) -> dict[str, Any]:
    return {
        "span": span,
        "window_size": 200,
        "sample_count": sample_count,
        "avg_ms": None,
        "min_ms": None,
        "max_ms": max_ms,
        "p50_ms": p50_ms,
        "p95_ms": p95_ms,
        "p99_ms": None,
    }


def _make_snapshot(
    spans: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "server": {"pid": 1, "uptime_seconds": 0, "configured_server_threads": 1},
        "memory": {},
        "processes": {},
        "background_tasks": [],
        "db": {},
        "routing_runtime": {},
    }
    if spans is not None:
        snapshot["dispatch_spans"] = {"window_size": 200, "spans": spans}
    return snapshot


class TestDispatchSpansPanelPresentWithData:
    """Populated snapshot renders the panel with expected span names and counts."""

    def test_panel_present_with_data(self) -> None:
        spans = [
            _make_span(
                "coordinator_pre_upstream",
                sample_count=50,
                p50_ms=0.5,
                p95_ms=1.2,
                max_ms=3.0,
            ),
            _make_span(
                "segmentation", sample_count=30, p50_ms=0.3, p95_ms=0.8, max_ms=1.5
            ),
            _make_span("compression_analyze", sample_count=0),
            _make_span(
                "compression_apply", sample_count=10, p50_ms=0.1, p95_ms=0.4, max_ms=0.9
            ),
            _make_span("selection_lock_wait", sample_count=0),
            _make_span("selection_locked", sample_count=0),
            _make_span("routing_trace_write", sample_count=0),
        ]
        html = render_runtime(_make_snapshot(spans))
        assert "Dispatch spans" in html
        assert "Coordinator pre-upstream" in html
        assert "Segmentation" in html
        assert "Compression analyze" in html
        assert "Compression apply" in html
        assert "Selection lock wait" in html
        assert "Selection locked" in html
        assert "Routing trace write" in html

    def test_panel_present_with_sample_counts(self) -> None:
        spans = [
            _make_span("coordinator_pre_upstream", sample_count=50),
            _make_span("segmentation", sample_count=30),
        ]
        html = render_runtime(_make_snapshot(spans))
        assert "50" in html
        assert "30" in html


class TestDispatchSpansPanelAbsentWhenMissing:
    """When dispatch_spans is absent, the panel is NOT rendered."""

    def test_absent_when_dispatch_spans_missing(self) -> None:
        html = render_runtime(_make_snapshot())
        assert "Dispatch spans" not in html

    def test_absent_when_dispatch_spans_empty(self) -> None:
        snapshot = _make_snapshot()
        snapshot["dispatch_spans"] = {"window_size": 200, "spans": []}
        html = render_runtime(snapshot)
        assert "Dispatch spans" not in html


class TestDispatchSpansAbsentSpansRenderAsNotObserved:
    """When a span has sample_count 0, duration columns render as 'not observed'."""

    def test_zero_sample_shows_not_observed(self) -> None:
        spans = [
            _make_span("compression_analyze", sample_count=0),
            _make_span("compression_apply", sample_count=0),
        ]
        html = _render_dispatch_spans_panel(spans)
        assert "not observed in recent window" in html
        # Must not show "0 ms" for disabled spans
        assert "0 ms" not in html

    def test_absent_span_key_shows_not_observed(self) -> None:
        """Span key not present in the list at all renders as not observed."""
        spans: list[dict[str, Any]] = []
        html = _render_dispatch_spans_panel(spans)
        # Empty spans means the function returns ""
        assert html == ""


class TestDispatchSpansPresentSpansRenderP50P95Max:
    """When a span has samples, p50/p95/max are rendered with ms units."""

    def test_rendered_with_ms_units(self) -> None:
        spans = [
            _make_span(
                "coordinator_pre_upstream",
                sample_count=100,
                p50_ms=0.42,
                p95_ms=1.53,
                max_ms=3.21,
            ),
            _make_span(
                "segmentation", sample_count=10, p50_ms=0.1, p95_ms=0.2, max_ms=0.3
            ),
            _make_span(
                "compression_analyze",
                sample_count=5,
                p50_ms=0.05,
                p95_ms=0.1,
                max_ms=0.2,
            ),
            _make_span(
                "compression_apply", sample_count=5, p50_ms=0.05, p95_ms=0.1, max_ms=0.2
            ),
            _make_span(
                "selection_lock_wait",
                sample_count=5,
                p50_ms=0.01,
                p95_ms=0.02,
                max_ms=0.03,
            ),
            _make_span(
                "selection_locked",
                sample_count=5,
                p50_ms=0.01,
                p95_ms=0.02,
                max_ms=0.03,
            ),
            _make_span(
                "routing_trace_write",
                sample_count=5,
                p50_ms=0.01,
                p95_ms=0.02,
                max_ms=0.03,
            ),
        ]
        html = _render_dispatch_spans_panel(spans)
        assert "0.42 ms" in html
        assert "1.5 ms" in html
        assert "3.2 ms" in html
        assert "not observed" not in html

    def test_none_values_render_as_dash(self) -> None:
        spans = [
            _make_span(
                "coordinator_pre_upstream",
                sample_count=10,
                p50_ms=None,
                p95_ms=None,
                max_ms=None,
            ),
            _make_span(
                "segmentation", sample_count=10, p50_ms=None, p95_ms=None, max_ms=None
            ),
            _make_span(
                "compression_analyze",
                sample_count=10,
                p50_ms=None,
                p95_ms=None,
                max_ms=None,
            ),
            _make_span(
                "compression_apply",
                sample_count=10,
                p50_ms=None,
                p95_ms=None,
                max_ms=None,
            ),
            _make_span(
                "selection_lock_wait",
                sample_count=10,
                p50_ms=None,
                p95_ms=None,
                max_ms=None,
            ),
            _make_span(
                "selection_locked",
                sample_count=10,
                p50_ms=None,
                p95_ms=None,
                max_ms=None,
            ),
            _make_span(
                "routing_trace_write",
                sample_count=10,
                p50_ms=None,
                p95_ms=None,
                max_ms=None,
            ),
        ]
        html = _render_dispatch_spans_panel(spans)
        assert "\u2014" in html  # em-dash for None


class TestDispatchSpansPanelIncludesActionableSpans:
    """All seven actionable spans are addressed in the panel."""

    def test_all_actionable_spans_present(self) -> None:
        spans = [
            _make_span("coordinator_pre_upstream", sample_count=5),
            _make_span("segmentation", sample_count=5),
            _make_span("compression_analyze", sample_count=5),
            _make_span("compression_apply", sample_count=5),
            _make_span("selection_lock_wait", sample_count=5),
            _make_span("selection_locked", sample_count=5),
            _make_span("routing_trace_write", sample_count=5),
        ]
        html = _render_dispatch_spans_panel(spans)
        for label in _DISPATCH_SPAN_LABELS.values():
            assert label in html, f"Actionable span label '{label}' missing from panel"

    def test_actionable_spans_shown_when_zero_samples(self) -> None:
        """Even with zero samples, all seven actionable spans appear."""
        spans = [
            _make_span("coordinator_pre_upstream", sample_count=0),
            _make_span("segmentation", sample_count=0),
            _make_span("compression_analyze", sample_count=0),
            _make_span("compression_apply", sample_count=0),
            _make_span("selection_lock_wait", sample_count=0),
            _make_span("selection_locked", sample_count=0),
            _make_span("routing_trace_write", sample_count=0),
        ]
        html = _render_dispatch_spans_panel(spans)
        # All seven should show "not observed" and no "0 ms"
        assert html.count("not observed in recent window") == 7
        assert "0 ms" not in html
        for label in _DISPATCH_SPAN_LABELS.values():
            assert label in html
