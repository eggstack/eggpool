"""Tests for the dispatch spans panel in the runtime dashboard."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from eggpool.api.runtime import register_runtime_routes
from eggpool.dashboard.render import (
    _DISPATCH_SPAN_LABELS,
    _render_dispatch_spans_panel,
    render_runtime,
)
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.models.config import AppConfig
from eggpool.runtime_metrics import RuntimeMetricsService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


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


# ---------------------------------------------------------------------------
# End-to-end API + dashboard-rendering integration
# ---------------------------------------------------------------------------


def _build_runtime_config() -> AppConfig:
    config = AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "OPENCODE_TEST_KEY",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": "http://localhost:19999"},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [{"name": "test-acct", "api_key_env": "OPENCODE_TEST_KEY"}],
            "dashboard": {"enabled": False},
        }
    )
    config.server.api_key = "test-key-12345678"
    return config


@pytest_asyncio.fixture()
async def runtime_db(tmp_path: Any) -> AsyncGenerator[Database, None]:
    database = Database(path=str(tmp_path / "test.sqlite3"))
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    yield database
    await database.disconnect()


def _make_runtime_app(db: Database, *, dispatch_span_recorder: Any = None) -> FastAPI:
    config = _build_runtime_config()
    app = FastAPI()
    app.state.db = db
    app.state.stats_db = db
    app.state.config = config
    app.state.runtime_metrics = RuntimeMetricsService(
        config=config,
        db=db,
        stats_db=db,
        supervisor=None,
        task_monitor=None,
        router=None,
        health_manager=None,
        started_monotonic=time.monotonic() - 60.0,
        started_epoch=time.time() - 60.0,
        dispatch_span_recorder=dispatch_span_recorder,
    )
    register_runtime_routes(app)
    return app


class TestDispatchSpansApiEndToEnd:
    """Pins the ``dispatch_spans`` JSON shape via ``GET /api/stats/runtime``.

    The endpoint is always auth-gated, so the test supplies the
    ``Bearer`` header explicitly.
    """

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer test-key-12345678"}

    def test_runtime_api_returns_dispatch_spans(self, runtime_db: Database) -> None:
        """The runtime API must include the full ``dispatch_spans`` payload."""
        from eggpool.runtime_dispatch import (
            SPAN_COMPRESSION_APPLY,
            SPAN_COORDINATOR_PRE_UPSTREAM,
            SPAN_SEGMENTATION,
            DispatchSpanRecorder,
        )

        recorder = DispatchSpanRecorder(window_size=200)
        recorder.record_ns(SPAN_COORDINATOR_PRE_UPSTREAM, 12_000_000)  # 12 ms
        recorder.record_ns(SPAN_COORDINATOR_PRE_UPSTREAM, 14_000_000)  # 14 ms
        recorder.record_ns(SPAN_SEGMENTATION, 8_000_000)  # 8 ms
        recorder.record_ns(SPAN_COMPRESSION_APPLY, 22_000_000)  # 22 ms

        app = _make_runtime_app(runtime_db, dispatch_span_recorder=recorder)
        client = TestClient(app)
        response = client.get("/api/stats/runtime", headers=self._auth_headers())
        assert response.status_code == 200
        body = response.json()
        dispatch_spans = body["dispatch_spans"]
        assert dispatch_spans["window_size"] == 200
        spans_by_key = {row["span"]: row for row in dispatch_spans["spans"]}

        # Recorded spans retain duration fields.
        cpu = spans_by_key[SPAN_COORDINATOR_PRE_UPSTREAM]
        assert cpu["sample_count"] == 2
        assert cpu["min_ms"] == pytest.approx(12.0)
        assert cpu["max_ms"] == pytest.approx(14.0)
        assert cpu["p50_ms"] is not None
        assert cpu["p95_ms"] is not None

        seg = spans_by_key[SPAN_SEGMENTATION]
        assert seg["sample_count"] == 1
        assert seg["avg_ms"] == pytest.approx(8.0)

        apply = spans_by_key[SPAN_COMPRESSION_APPLY]
        assert apply["sample_count"] == 1
        assert apply["avg_ms"] == pytest.approx(22.0)

    def test_runtime_api_marks_absent_spans_with_zero_count(
        self, runtime_db: Database
    ) -> None:
        """Spans with no recorded samples must appear with sample_count == 0
        and ``None`` numeric fields, **not** zero-valued samples."""
        from eggpool.runtime_dispatch import (
            SPAN_COMPRESSION_ANALYZE,
            SPAN_COMPRESSION_APPLY,
            DispatchSpanRecorder,
        )

        recorder = DispatchSpanRecorder(window_size=200)
        recorder.record_ns(SPAN_COMPRESSION_APPLY, 5_000_000)
        app = _make_runtime_app(runtime_db, dispatch_span_recorder=recorder)
        client = TestClient(app)
        response = client.get("/api/stats/runtime", headers=self._auth_headers())
        assert response.status_code == 200
        body = response.json()
        spans_by_key = {row["span"]: row for row in body["dispatch_spans"]["spans"]}
        analyze = spans_by_key[SPAN_COMPRESSION_ANALYZE]
        assert analyze["sample_count"] == 0
        assert analyze["avg_ms"] is None
        assert analyze["p50_ms"] is None
        assert analyze["p95_ms"] is None
        assert analyze["max_ms"] is None
        apply = spans_by_key[SPAN_COMPRESSION_APPLY]
        assert apply["sample_count"] == 1
        assert apply["avg_ms"] is not None


class TestDispatchSpansDashboardEndToEnd:
    """Pins the dashboard HTML for a snapshot produced from the API."""

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer test-key-12345678"}

    def test_api_snapshot_renders_dashboard_with_empty_state(
        self, runtime_db: Database
    ) -> None:
        """Snapshot returned by ``/api/stats/runtime`` with apply only
        should render the Dispatch spans panel with one populated span
        and the missing analyze span rendered as "not observed in
        recent window", not as ``0 ms``."""
        from eggpool.runtime_dispatch import (
            SPAN_COMPRESSION_APPLY,
            SPAN_SEGMENTATION,
            DispatchSpanRecorder,
        )

        recorder = DispatchSpanRecorder(window_size=200)
        recorder.record_ns(SPAN_SEGMENTATION, 6_000_000)
        recorder.record_ns(SPAN_COMPRESSION_APPLY, 10_000_000)
        app = _make_runtime_app(runtime_db, dispatch_span_recorder=recorder)
        client = TestClient(app)
        response = client.get("/api/stats/runtime", headers=self._auth_headers())
        assert response.status_code == 200
        snapshot = response.json()
        html = render_runtime(snapshot)

        assert "Dispatch spans" in html
        # Compression apply is populated.
        assert "Compression apply" in html
        # Compression analyze absent → "not observed in recent window".
        assert "not observed in recent window" in html
        # The duration columns for any zero-sample span must NOT contain a
        # "0 ms" (or "0.0 ms") render.  The formatter emits "—" for None
        # and "<n>.<frac> ms" for finite numbers.  We assert on the panel
        # substring (between the section anchors) to avoid unrelated
        # cells elsewhere in the page.
        panel_start = html.find("<h3>Dispatch spans</h3>")
        panel_end = html.find("</section>", panel_start)
        assert panel_start > 0 and panel_end > panel_start
        panel_html = html[panel_start:panel_end]
        # No naked `0 ms` or `0.0 ms` in the panel.
        assert ">0 ms<" not in panel_html
        assert ">0.0 ms<" not in panel_html
        # Apply span row has a real numeric ms value.
        apply_row_idx = panel_html.find("Compression apply")
        assert apply_row_idx >= 0
        apply_row_end = panel_html.find("</tr>", apply_row_idx)
        apply_row = panel_html[apply_row_idx:apply_row_end]
        assert "10" in apply_row and "ms" in apply_row

    def test_api_snapshot_renders_dashboard_with_populated_spans(
        self, runtime_db: Database
    ) -> None:
        """When multiple spans have samples, every actionable span label
        is rendered, and the populated rows show numeric ms while
        zero-sample actionable spans show ``not observed in recent
        window``."""
        from eggpool.runtime_dispatch import (
            SPAN_COMPRESSION_APPLY,
            SPAN_COORDINATOR_PRE_UPSTREAM,
            SPAN_SEGMENTATION,
            DispatchSpanRecorder,
        )

        recorder = DispatchSpanRecorder(window_size=200)
        recorder.record_ns(SPAN_COORDINATOR_PRE_UPSTREAM, 4_000_000)
        recorder.record_ns(SPAN_SEGMENTATION, 6_000_000)
        recorder.record_ns(SPAN_COMPRESSION_APPLY, 9_000_000)
        app = _make_runtime_app(runtime_db, dispatch_span_recorder=recorder)
        client = TestClient(app)
        response = client.get("/api/stats/runtime", headers=self._auth_headers())
        snapshot = response.json()
        html = render_runtime(snapshot)

        assert "Dispatch spans" in html
        for label in _DISPATCH_SPAN_LABELS.values():
            assert label in html, f"Missing actionable span label: {label}"
        # The three populated spans show numeric ms; the four zero-sample
        # actionable spans (``compression_analyze``, ``selection_lock_wait``,
        # ``selection_locked``, ``routing_trace_write``) show ``not observed``.
        assert html.count("not observed in recent window") == 4
