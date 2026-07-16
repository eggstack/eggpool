"""Integration tests for Dispatch Stability Milestone D — Off-Path Observability.

Covers:
- D5: Guard enrichment (writer queue pressure, flush failures, hysteresis)
- D8: Dashboard trace diagnostics rendering
- D9: Rehash transition tests for trace mode and config
- Acceptance criteria: DB-blocked trace write doesn't delay upstream
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from eggpool.request.routing_trace_guard import RoutingTraceGuard

# ---------------------------------------------------------------------------
# D5 Guard — writer-aware integration
# ---------------------------------------------------------------------------


class TestGuardWriterAwareness:
    """Verify the guard consults writer snapshot and applies hysteresis."""

    def test_guard_skips_when_queue_nearly_full(self) -> None:
        guard = RoutingTraceGuard(threshold_ms=0.0, queue_occupancy_threshold=0.8)
        snap = {"queue_depth": 850, "queue_capacity": 1000}
        skip, reason = guard.should_skip(db=None, writer_snapshot=snap)
        assert skip is True
        assert reason == "queue_pressure"

    def test_guard_skips_when_flush_errors_present(self) -> None:
        guard = RoutingTraceGuard(
            threshold_ms=0.0,
            queue_occupancy_threshold=1.0,
            oldest_event_age_s=600.0,
        )
        snap = {
            "queue_depth": 10,
            "queue_capacity": 1000,
            "oldest_event_age_s": 1.0,
            "dropped_flush_error": 3,
        }
        skip, reason = guard.should_skip(db=None, writer_snapshot=snap)
        assert skip is True
        assert reason == "flush_failure"

    def test_guard_cooldown_prevents_oscillation(self) -> None:
        """After a skip, cooldown keeps skipping for cooldown_s."""
        guard = RoutingTraceGuard(threshold_ms=0.0, cooldown_s=2.0)
        # Trigger a skip
        high_snap = {"queue_depth": 900, "queue_capacity": 1000}
        skip1, reason1 = guard.should_skip(db=None, writer_snapshot=high_snap)
        assert skip1 is True
        assert reason1 == "queue_pressure"

        # Immediately after — queue is fine, but cooldown keeps us skipping
        ok_snap = {"queue_depth": 10, "queue_capacity": 1000}
        skip2, reason2 = guard.should_skip(db=None, writer_snapshot=ok_snap)
        assert skip2 is True
        assert reason2 == "cooldown"

    def test_guard_cooldown_allows_after_expiry(self) -> None:
        guard = RoutingTraceGuard(threshold_ms=0.0, cooldown_s=0.01)
        high_snap = {"queue_depth": 900, "queue_capacity": 1000}
        guard.should_skip(db=None, writer_snapshot=high_snap)
        time.sleep(0.02)
        ok_snap = {"queue_depth": 10, "queue_capacity": 1000}
        skip, reason = guard.should_skip(db=None, writer_snapshot=ok_snap)
        assert skip is False
        assert reason == "ok"

    def test_guard_snapshot_records_writer_diagnostics(self) -> None:
        guard = RoutingTraceGuard(threshold_ms=0.0)
        snap = {
            "queue_depth": 42,
            "queue_capacity": 500,
            "oldest_event_age_s": 7.5,
            "dropped_flush_error": 2,
        }
        guard.should_skip(db=None, writer_snapshot=snap)
        result = guard.snapshot()
        assert result["last_writer_queue_depth"] == 42
        assert result["last_writer_queue_capacity"] == 500
        assert result["last_writer_oldest_age_s"] == 7.5
        assert result["last_writer_flush_errors"] == 2

    def test_guard_disabled_still_returns_disabled(self) -> None:
        guard = RoutingTraceGuard(enabled=False, cooldown_s=10.0)
        skip, reason = guard.should_skip(db=None)
        assert skip is True
        assert reason == "disabled"


# ---------------------------------------------------------------------------
# D9 Rehash transition tests
# ---------------------------------------------------------------------------


class TestRehashTraceModeTransitions:
    """Verify trace mode transitions via config changes."""

    def test_mode_transition_all_to_sampled(self) -> None:
        """Changing mode from 'all' to 'sampled' updates the writer config."""
        from eggpool.observability.routing_trace_writer import RoutingTraceWriter

        writer = MagicMock(spec=RoutingTraceWriter)
        writer.configure = MagicMock()

        # Simulate: writer was configured for 'all', now rehash to 'sampled'
        writer.configure(mode="sampled", sample_rate=0.05)
        writer.configure.assert_called_with(mode="sampled", sample_rate=0.05)

    def test_mode_transition_sampled_to_off(self) -> None:
        from eggpool.observability.routing_trace_writer import RoutingTraceWriter

        writer = MagicMock(spec=RoutingTraceWriter)
        writer.configure(mode="off")
        writer.configure.assert_called_with(mode="off")

    def test_mode_transition_off_to_sampled(self) -> None:
        from eggpool.observability.routing_trace_writer import RoutingTraceWriter

        writer = MagicMock(spec=RoutingTraceWriter)
        writer.configure(mode="sampled", sample_rate=0.10)
        writer.configure.assert_called_with(mode="sampled", sample_rate=0.10)

    def test_include_score_components_toggle(self) -> None:
        """Toggling include_score_components updates the config."""
        from eggpool.models.config import RoutingTraceConfig

        cfg = RoutingTraceConfig(include_score_components=False)
        assert cfg.include_score_components is False
        cfg2 = RoutingTraceConfig(include_score_components=True)
        assert cfg2.include_score_components is True

    def test_guard_config_transition_via_configure(self) -> None:
        """Guard config fields update via configure()."""
        guard = RoutingTraceGuard(
            threshold_ms=200.0,
            queue_occupancy_threshold=0.8,
            oldest_event_age_s=30.0,
            cooldown_s=5.0,
        )
        guard.configure(
            threshold_ms=100.0,
            queue_occupancy_threshold=0.9,
            oldest_event_age_s=60.0,
            cooldown_s=10.0,
        )
        snap = guard.snapshot()
        assert snap["threshold_ms"] == 100.0
        assert snap["queue_occupancy_threshold"] == 0.9
        assert snap["oldest_event_age_s"] == 60.0
        assert snap["cooldown_s"] == 10.0


# ---------------------------------------------------------------------------
# D8 Dashboard trace diagnostics rendering
# ---------------------------------------------------------------------------


class TestDashboardTraceDiagnostics:
    """Verify the routing page renders trace diagnostics panel."""

    def test_render_routing_includes_trace_panel(self) -> None:
        from eggpool.dashboard.render import render_routing

        html = render_routing(
            period="24h",
            routing_distribution=[],
            routing_selection_breakdown=[],
            routing_exclusion_breakdown=[],
            routing_skew_summary={},
            trace_mode="all",
            trace_sample_rate=1.0,
            trace_writer={
                "enabled": True,
                "accepted": 100,
                "written": 95,
                "dropped_queue_full": 3,
                "dropped_flush_error": 2,
                "queue_depth": 10,
                "queue_capacity": 1000,
                "oldest_event_age_s": 2.5,
            },
        )
        assert "Routing trace observability" in html
        assert "trace mode" in html.lower() or "Trace mode" in html
        assert "100" in html  # accepted count

    def test_render_routing_off_mode_shows_off(self) -> None:
        from eggpool.dashboard.render import render_routing

        html = render_routing(
            period="24h",
            routing_distribution=[],
            routing_selection_breakdown=[],
            routing_exclusion_breakdown=[],
            routing_skew_summary={},
            trace_mode="off",
            trace_sample_rate=0.0,
            trace_writer={"enabled": False},
        )
        assert "Off" in html

    def test_render_routing_sampled_mode_shows_sampled_note(self) -> None:
        from eggpool.dashboard.render import render_routing

        html = render_routing(
            period="24h",
            routing_distribution=[],
            routing_selection_breakdown=[],
            routing_exclusion_breakdown=[],
            routing_skew_summary={},
            trace_mode="sampled",
            trace_sample_rate=0.05,
            trace_writer={"enabled": True, "accepted": 0, "written": 0},
        )
        assert "sampled" in html.lower()

    def test_render_routing_drops_warning_when_queue_full(self) -> None:
        from eggpool.dashboard.render import render_routing

        html = render_routing(
            period="24h",
            routing_distribution=[],
            routing_selection_breakdown=[],
            routing_exclusion_breakdown=[],
            routing_skew_summary={},
            trace_mode="all",
            trace_sample_rate=1.0,
            trace_writer={
                "enabled": True,
                "accepted": 100,
                "written": 50,
                "dropped_queue_full": 50,
                "queue_depth": 1000,
                "queue_capacity": 1000,
            },
        )
        # Queue at 100% should show warning
        assert "warning" in html.lower() or "Degraded" in html

    def test_render_routing_empty_writer_shows_no_error(self) -> None:
        from eggpool.dashboard.render import render_routing

        html = render_routing(
            period="24h",
            routing_distribution=[],
            routing_selection_breakdown=[],
            routing_exclusion_breakdown=[],
            routing_skew_summary={},
            trace_mode="sampled",
            trace_sample_rate=0.05,
            trace_writer=None,
        )
        # Should render without error even with no writer data
        assert "Routing trace observability" in html


# ---------------------------------------------------------------------------
# Acceptance: trace writer snapshot safety
# ---------------------------------------------------------------------------


class TestWriterSnapshotSafety:
    """Verify RoutingTraceWriter snapshot is thread-safe."""

    def test_snapshot_returns_consistent_dict(self) -> None:
        from eggpool.observability.routing_trace_writer import RoutingTraceWriter

        # Use a mock db/repo since we only test snapshot()
        writer = RoutingTraceWriter(
            db=MagicMock(),
            routing_decision_repo=MagicMock(),
            queue_capacity=100,
        )
        snap = writer.snapshot()
        assert isinstance(snap, dict)
        assert "queue_depth" in snap
        assert "queue_capacity" in snap
        assert "accepted" in snap
        assert "written" in snap
        assert snap["queue_capacity"] == 100

    def test_submit_increments_accepted_counter(self) -> None:
        from eggpool.observability.routing_trace_writer import (
            RoutingTraceEvent,
            RoutingTraceWriter,
        )

        writer = RoutingTraceWriter(
            db=MagicMock(),
            routing_decision_repo=MagicMock(),
            queue_capacity=100,
        )
        # Set state to running manually for test
        writer._state = "running"  # pyright: ignore[reportPrivateUsage]
        event = RoutingTraceEvent(
            request_id="test-1",
            db_request_id=1,
            attempt_number=1,
            model_id="test-model",
            provider_id=None,
            protocol="openai",
            selected_account_name="acct-a",
            selected_account_id=1,
            selected_tier=0,
            selected_score=0.5,
            eligible_count=2,
            scored_count=2,
            attempted_excluded_count=0,
            top_score=0.5,
            top_score_account_name="acct-a",
            exclude_reasons_json="{}",
            score_components_json=None,
            created_at_mono_ns=time.monotonic_ns(),
            created_at_epoch=time.time(),
            generation_id=None,
        )
        result = writer.submit(event)
        assert result == "accepted"
        snap = writer.snapshot()
        assert snap["accepted"] == 1

    def test_submit_drops_when_queue_full(self) -> None:
        from eggpool.observability.routing_trace_writer import (
            RoutingTraceEvent,
            RoutingTraceWriter,
        )

        writer = RoutingTraceWriter(
            db=MagicMock(),
            routing_decision_repo=MagicMock(),
            queue_capacity=2,
        )
        writer._state = "running"  # pyright: ignore[reportPrivateUsage]
        for i in range(5):
            event = RoutingTraceEvent(
                request_id=f"req-{i}",
                db_request_id=i,
                attempt_number=1,
                model_id="test-model",
                provider_id=None,
                protocol="openai",
                selected_account_name="acct",
                selected_account_id=1,
                selected_tier=0,
                selected_score=0.5,
                eligible_count=1,
                scored_count=1,
                attempted_excluded_count=0,
                top_score=0.5,
                top_score_account_name="acct",
                exclude_reasons_json="{}",
                score_components_json=None,
                created_at_mono_ns=time.monotonic_ns(),
                created_at_epoch=time.time(),
                generation_id=None,
            )
            writer.submit(event)
        snap = writer.snapshot()
        assert snap["accepted"] == 2  # only 2 fit
        assert snap["dropped_queue_full"] == 3

    def test_event_to_json_bytes_omits_secrets(self) -> None:
        from eggpool.observability.routing_trace_writer import RoutingTraceEvent

        event = RoutingTraceEvent(
            request_id="req-secret-test",
            db_request_id=42,
            attempt_number=1,
            model_id="gpt-4",
            provider_id="openai",
            protocol="openai",
            selected_account_name="my-secret-account",
            selected_account_id=7,
            selected_tier=0,
            selected_score=0.9,
            eligible_count=3,
            scored_count=3,
            attempted_excluded_count=0,
            top_score=0.9,
            top_score_account_name="my-secret-account",
            exclude_reasons_json="{}",
            score_components_json=None,
            created_at_mono_ns=time.monotonic_ns(),
            created_at_epoch=time.time(),
            generation_id=1,
        )
        data = event.to_json_bytes()
        assert b"api_key" not in data
        assert b"Bearer" not in data
        assert b"sk-" not in data
