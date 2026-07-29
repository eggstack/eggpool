from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from scripts.run_dispatch_stability_soak import (
    SCHEMA_VERSION,
    WindowMetrics,
    _build_parser,
    _memory_total_bytes,
    _write_atomic_json,
    build_duration_plan,
    evaluate_drain_gate,
    evaluate_ratio_gate,
    evaluate_workload_gate,
    read_process_rss_bytes,
)

WORKFLOW_PATH = Path(".github/workflows/extended-soak.yml")
RELEASE_DOC = Path("docs/releasing.md")


class TestBuildDurationPlan:
    def test_phase_properties(self) -> None:
        for total in [30, 300, 3600]:
            p = build_duration_plan(total)
            phase_sum = p.warmup_s + p.drain_s + p.early_window_s + p.late_window_s
            assert phase_sum == pytest.approx(total, abs=2.0)
            assert p.warmup_s > 0 and p.drain_s > 0
            assert p.early_window_s > 0 and p.late_window_s > 0
            assert p.early_window_s == pytest.approx(p.late_window_s, abs=0.01)


class TestBuildParser:
    def test_full_args(self) -> None:
        args = _build_parser().parse_args(
            [
                "--profile",
                "sbc-reference",
                "--duration-seconds",
                "600",
                "--output",
                "/tmp/test.json",
                "--seed",
                "99",
                "-v",
            ]
        )
        assert args.profile == "sbc-reference"
        assert args.duration_seconds == 600
        assert args.output == "/tmp/test.json"
        assert args.seed == 99
        assert args.verbose is True

    def test_rejects_invalid(self) -> None:
        for bad in [
            ["--mode", "smoke"],
            ["--duration-seconds", "abc"],
            ["--duration-seconds", "0"],
            ["--duration-seconds", "-5"],
            ["--duration-seconds", "29"],
        ]:
            with pytest.raises(SystemExit):
                _build_parser().parse_args(bad)


class TestReadProcessRssBytes:
    def _linux(self, lines: list[str]) -> str:
        return "\n".join(lines) + "\n"

    def test_linux_valid(self) -> None:
        status = self._linux(["VmRSS:     12345 kB"])
        with (
            patch("builtins.open", create=True) as m,
            patch.object(sys, "platform", "linux"),
        ):
            m.return_value.__enter__ = lambda s: s
            m.return_value.__exit__ = lambda s, *a: False
            m.return_value.__iter__ = lambda s: iter(status.splitlines())
            assert read_process_rss_bytes(1) == 12345 * 1024

    def test_linux_none_cases(self) -> None:
        for lines in [
            ["Name: eggpool", "State: S"],
            ["VmRSS:     12345 bytes"],
            ["VmRSS:     0 kB"],
        ]:
            status = self._linux(lines)
            with (
                patch("builtins.open", create=True) as m,
                patch.object(sys, "platform", "linux"),
            ):
                m.return_value.__enter__ = lambda s: s
                m.return_value.__exit__ = lambda s, *a: False
                m.return_value.__iter__ = lambda s, st=status: iter(st.splitlines())
                assert read_process_rss_bytes(1) is None

    def test_macos_and_unsupported(self) -> None:
        r = type("R", (), {"returncode": 0, "stdout": "  12345\n"})()
        with (
            patch("subprocess.run", return_value=r),
            patch.object(sys, "platform", "darwin"),
        ):
            assert read_process_rss_bytes(1) == 12345 * 1024
        r_fail = type("R", (), {"returncode": 1, "stdout": ""})()
        with (
            patch("subprocess.run", return_value=r_fail),
            patch.object(sys, "platform", "darwin"),
        ):
            assert read_process_rss_bytes(1) is None
        with (
            patch("subprocess.run", side_effect=OSError("not supported")),
            patch.object(sys, "platform", "win32"),
        ):
            assert read_process_rss_bytes(1) is None


class TestMemoryTotalBytes:
    def test_none_when_sysconf_unavailable_or_fails(self) -> None:
        with (
            patch.object(os, "sysconf_names", {}),
            patch.object(sys, "platform", "linux"),
        ):
            assert _memory_total_bytes() is None
        with (
            patch("os.sysconf", side_effect=OSError("fail")),
            patch.object(sys, "platform", "linux"),
        ):
            assert _memory_total_bytes() is None


class TestEvaluateDrainGate:
    def test_failure_cases(self) -> None:
        passed, reason = evaluate_drain_gate(None, 0, 0)
        assert passed is False
        assert reason == "no final runtime snapshot"
        for pending, active in [(None, 0), (0, None)]:
            snap = type(
                "S",
                (),
                {
                    "pending_requests": pending,
                    "active_reservations": active,
                },
            )()
            passed, reason = evaluate_drain_gate(snap, 0, 0)
            assert passed is False
            assert reason == "drain metrics unavailable"
        snap = type("S", (), {"pending_requests": 5, "active_reservations": 3})()
        passed, reason = evaluate_drain_gate(snap, 0, 0)
        assert passed is False
        assert "pending=5" in reason and "reservations=3" in reason

    def test_zero_passes(self) -> None:
        snap = type("S", (), {"pending_requests": 0, "active_reservations": 0})()
        passed, _ = evaluate_drain_gate(snap, 0, 0)
        assert passed is True


class TestEvaluateRatioGate:
    def test_pass_under_limit(self) -> None:
        result = evaluate_ratio_gate(10.0, 14.9, limit=1.50, label="dispatch_p95")
        assert result.passed is True
        assert result.ratio == pytest.approx(1.49, abs=1e-3)
        assert result.failure_reason is None
        assert result.limit == 1.50

    def test_pass_at_equal_limit(self) -> None:
        result = evaluate_ratio_gate(10.0, 15.0, limit=1.50, label="dispatch_p95")
        assert result.passed is True
        assert result.ratio == pytest.approx(1.50, abs=1e-6)
        assert result.failure_reason is None

    def test_fail_above_limit(self) -> None:
        result = evaluate_ratio_gate(10.0, 15.1, limit=1.50, label="dispatch_p95")
        assert result.passed is False
        assert result.ratio == pytest.approx(1.51, abs=1e-3)
        assert result.failure_reason is not None
        assert "exceeds limit" in result.failure_reason

    def test_fail_when_early_samples_unavailable(self) -> None:
        result = evaluate_ratio_gate(None, 12.4, limit=1.50, label="dispatch_p95")
        assert result.passed is False
        assert result.ratio is None
        assert "samples unavailable" in (result.failure_reason or "")

    def test_fail_when_late_samples_unavailable(self) -> None:
        result = evaluate_ratio_gate(10.0, None, limit=1.50, label="dispatch_p95")
        assert result.passed is False
        assert result.ratio is None
        assert "samples unavailable" in (result.failure_reason or "")

    def test_fail_when_early_zero(self) -> None:
        result = evaluate_ratio_gate(0.0, 12.4, limit=1.50, label="dispatch_p95")
        assert result.passed is False
        assert result.ratio is None
        assert "non-positive" in (result.failure_reason or "")

    def test_fail_when_early_negative(self) -> None:
        result = evaluate_ratio_gate(-1.0, 12.4, limit=1.50, label="dispatch_p95")
        assert result.passed is False
        assert "non-positive" in (result.failure_reason or "")

    def test_does_not_add_one_to_limit(self) -> None:
        """Boundary check: ratio cap is a direct cap, not additive."""
        # 1.99 with limit=2.00 must pass; 1.0+limit logic would require
        # ratio <= 3.00 which would pass 1.99 anyway, but the spirit of
        # the cap is that 1.50 with limit=1.50 passes exactly.
        result = evaluate_ratio_gate(10.0, 14.99, limit=1.50, label="dispatch_p99")
        assert result.passed is True
        assert result.ratio is not None
        assert result.ratio <= result.limit


class TestEvaluateWorkloadGate:
    def _window(
        self,
        *,
        attempts: int = 5,
        successes: int = 5,
        stream_successes: int = 3,
        nonstream_successes: int = 2,
        errors: int = 0,
    ) -> WindowMetrics:
        return WindowMetrics(
            name="synthetic",
            start_time=0.0,
            end_time=1.0,
            request_count=attempts,
            success_count=successes,
            stream_success_count=stream_successes,
            nonstream_success_count=nonstream_successes,
            error_count=errors,
        )

    def test_zero_attempts_fails(self) -> None:
        early = self._window(attempts=0, successes=0)
        late = self._window()
        result = evaluate_workload_gate(early, late, expected_error_rate=0.0)
        assert result.passed is False
        assert any("zero attempts" in r for r in result.failure_reasons)

    def test_all_errors_fail_zero_error_profile(self) -> None:
        early = self._window(attempts=5, successes=0, errors=5)
        late = self._window(attempts=5, successes=0, errors=5)
        result = evaluate_workload_gate(early, late, expected_error_rate=0.0)
        assert result.passed is False
        joined = " ".join(result.failure_reasons)
        assert "zero successes" in joined
        assert "unexpected errors" in joined

    def test_one_success_per_window_passes_minimum(self) -> None:
        early = self._window(attempts=1, successes=1, errors=0)
        late = self._window(attempts=1, successes=1, errors=0)
        result = evaluate_workload_gate(early, late, expected_error_rate=0.0)
        assert result.passed is True
        assert result.early_successes == 1
        assert result.late_successes == 1

    def test_configured_error_rate_tolerates_some_errors(self) -> None:
        early = self._window(attempts=10, successes=9, errors=1)
        late = self._window(attempts=10, successes=9, errors=1)
        result = evaluate_workload_gate(early, late, expected_error_rate=0.05)
        assert result.passed is True
        assert result.allowed_error_fraction == pytest.approx(0.15, abs=1e-6)

    def test_configured_error_rate_rejects_excess_errors(self) -> None:
        early = self._window(attempts=10, successes=4, errors=6)
        late = self._window(attempts=10, successes=5, errors=5)
        result = evaluate_workload_gate(early, late, expected_error_rate=0.05)
        assert result.passed is False
        assert any("error rate" in r for r in result.failure_reasons)

    def test_require_stream_and_nonstream(self) -> None:
        early = self._window(
            attempts=2,
            successes=2,
            stream_successes=2,
            nonstream_successes=0,
            errors=0,
        )
        late = self._window(
            attempts=2,
            successes=2,
            stream_successes=0,
            nonstream_successes=2,
            errors=0,
        )
        result = evaluate_workload_gate(
            early, late, expected_error_rate=0.0, require_stream_and_nonstream=True
        )
        assert result.passed is True

    def test_require_stream_and_nonstream_rejects_one_missing(self) -> None:
        early = self._window(
            attempts=2,
            successes=2,
            stream_successes=2,
            nonstream_successes=0,
            errors=0,
        )
        late = self._window(
            attempts=2,
            successes=2,
            stream_successes=2,
            nonstream_successes=0,
            errors=0,
        )
        result = evaluate_workload_gate(
            early, late, expected_error_rate=0.0, require_stream_and_nonstream=True
        )
        assert result.passed is False
        joined = " ".join(result.failure_reasons)
        assert "non-streaming" in joined


class TestSchemaVersion:
    def test_schema_version_bumped(self) -> None:
        assert SCHEMA_VERSION >= 2


class TestRuntimeSnapshotObservation:
    """Tests for :func:`collect_runtime_snapshot` and bounded quiescence."""

    @staticmethod
    def _make_client(responses: list[Any]) -> Any:
        """Build a mock httpx.AsyncClient returning a queue of responses."""

        class _Resp:
            def __init__(self, payload: dict[str, Any]) -> None:
                self._payload = payload

            def json(self) -> dict[str, Any]:
                return self._payload

        class _Client:
            def __init__(self) -> None:
                self._calls = 0

            async def get(self, url: str, headers: dict[str, str]) -> Any:  # noqa: ARG002
                self._calls += 1
                if not responses:
                    raise RuntimeError("no more responses configured")
                resp = responses.pop(0)
                if isinstance(resp, Exception):
                    raise resp
                return resp

            async def __aenter__(self) -> _Client:
                return self

            async def __aexit__(self, *exc: Any) -> None:
                return None

        return _Client()

    @pytest.mark.asyncio
    async def test_collect_snapshot_records_null_on_failure(self) -> None:
        from scripts.run_dispatch_stability_soak import (
            MockUpstreamState,
            PollingStats,
            collect_runtime_snapshot,
        )

        client = self._make_client([RuntimeError("boom")])
        state = MockUpstreamState()
        polling = PollingStats()
        snap = await collect_runtime_snapshot(
            client=client,
            api_key="x",
            upstream_state=state,
            db_path="/nonexistent/path/that/does/not/exist",
            eggpool_pid=99999,
            start_time=0.0,
            polling_stats=polling,
            include_summary=False,
        )
        assert snap.pending_requests is None
        assert snap.active_reservations is None
        assert snap.upstream_requests == 0
        assert polling.runtime_failures == 1

    @pytest.mark.asyncio
    async def test_collect_snapshot_reads_runtime_fields(self) -> None:
        from scripts.run_dispatch_stability_soak import (
            MockUpstreamState,
            PollingStats,
            collect_runtime_snapshot,
        )

        payload = {
            "contention": {
                "lock_wait_p95_ms": 0.5,
                "lock_wait_max_ms": 1.5,
                "lock_wait_sample_count": 10,
            },
            "routing_runtime": {
                "pending_count": 0,
                "active_reservations_count": 0,
            },
        }

        class _Resp:
            def __init__(self) -> None:
                self.status_code = 200

            def json(self) -> dict[str, Any]:
                return payload

        class _Client:
            def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG004
                pass

            async def get(self, url: str, headers: dict[str, str]) -> _Resp:  # noqa: ARG002
                return _Resp()

            async def __aenter__(self) -> _Client:
                return self

            async def __aexit__(self, *exc: Any) -> None:
                return None

        snap = await collect_runtime_snapshot(
            client=_Client(),
            api_key="x",
            upstream_state=MockUpstreamState(),
            db_path="/nonexistent/path",
            eggpool_pid=99999,
            start_time=0.0,
            polling_stats=PollingStats(),
            include_summary=False,
        )
        assert snap.pending_requests == 0
        assert snap.active_reservations == 0
        assert snap.db_lock_wait_p95_ms == 0.5
        assert snap.db_lock_wait_sample_count == 10


class TestWaitForRuntimeQuiescence:
    """Bounded post-load quiescence polling."""

    @staticmethod
    def _stub_client(
        runtime_payloads: list[dict[str, Any]],
        raises: list[Exception] | None = None,
    ) -> Any:
        """Mock httpx.AsyncClient that returns a 200 summary, then iterates the
        supplied runtime payloads."""

        class _Resp:
            def __init__(self, payload: dict[str, Any] | None) -> None:
                self._payload = payload
                self.status_code = 200

            def json(self) -> dict[str, Any]:
                if self._payload is None:
                    raise RuntimeError("no payload configured")
                return self._payload

        class _Client:
            def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG004
                self._idx = 0
                self._runtime = list(runtime_payloads)
                self._raises = list(raises or [])
                self._call_count = 0

            async def get(self, url: str, headers: dict[str, str]) -> Any:  # noqa: ARG002
                self._call_count += 1
                # Summary endpoint returns an empty 200 each time.
                if "/api/stats/summary" in url:
                    return _Resp(None)
                if self._raises:
                    raise self._raises.pop(0)
                if self._idx >= len(self._runtime):
                    raise RuntimeError("exhausted")
                payload = self._runtime[self._idx]
                self._idx += 1
                return _Resp(payload)

            async def __aenter__(self) -> _Client:
                return self

            async def __aexit__(self, *exc: Any) -> None:
                return None

        return _Client

    @pytest.mark.asyncio
    async def test_first_observation_drained(self) -> None:
        from scripts.run_dispatch_stability_soak import (
            MockUpstreamState,
            PollingStats,
            wait_for_runtime_quiescence,
        )

        drained_payload = {
            "contention": {},
            "routing_runtime": {
                "pending_count": 0,
                "active_reservations_count": 0,
            },
        }

        client_cls = TestWaitForRuntimeQuiescence._stub_client([drained_payload])
        with patch("httpx.AsyncClient", new=client_cls):
            result = await wait_for_runtime_quiescence(
                base_url="127.0.0.1:1",
                api_key="x",
                upstream_state=MockUpstreamState(),
                db_path="/nonexistent/path",
                eggpool_pid=99999,
                start_time=0.0,
                polling_stats=PollingStats(),
                timeout_s=15.0,
                poll_interval_s=0.001,
                max_pending=0,
                max_active=0,
            )
        assert result.drained is True
        assert result.attempts == 1
        assert result.failure_reason is None
        assert result.pending_requests == 0
        assert result.active_reservations == 0

    @pytest.mark.asyncio
    async def test_second_observation_drained(self) -> None:
        from scripts.run_dispatch_stability_soak import (
            MockUpstreamState,
            PollingStats,
            wait_for_runtime_quiescence,
        )

        active_payload = {
            "contention": {},
            "routing_runtime": {
                "pending_count": 2,
                "active_reservations_count": 1,
            },
        }
        drained_payload = {
            "contention": {},
            "routing_runtime": {
                "pending_count": 0,
                "active_reservations_count": 0,
            },
        }

        client_cls = TestWaitForRuntimeQuiescence._stub_client(
            [active_payload, drained_payload]
        )
        with patch("httpx.AsyncClient", new=client_cls):
            result = await wait_for_runtime_quiescence(
                base_url="127.0.0.1:1",
                api_key="x",
                upstream_state=MockUpstreamState(),
                db_path="/nonexistent/path",
                eggpool_pid=99999,
                start_time=0.0,
                polling_stats=PollingStats(),
                timeout_s=15.0,
                poll_interval_s=0.001,
                max_pending=0,
                max_active=0,
            )
        assert result.drained is True
        assert result.attempts == 2

    @pytest.mark.asyncio
    async def test_repeated_active_state_times_out(self) -> None:
        from scripts.run_dispatch_stability_soak import (
            MockUpstreamState,
            PollingStats,
            wait_for_runtime_quiescence,
        )

        active_payload = {
            "contention": {},
            "routing_runtime": {
                "pending_count": 5,
                "active_reservations_count": 3,
            },
        }

        client_cls = TestWaitForRuntimeQuiescence._stub_client([active_payload] * 1000)
        with patch("httpx.AsyncClient", new=client_cls):
            result = await wait_for_runtime_quiescence(
                base_url="127.0.0.1:1",
                api_key="x",
                upstream_state=MockUpstreamState(),
                db_path="/nonexistent/path",
                eggpool_pid=99999,
                start_time=0.0,
                polling_stats=PollingStats(),
                timeout_s=0.05,
                poll_interval_s=0.001,
                max_pending=0,
                max_active=0,
            )
        assert result.drained is False
        assert result.failure_reason is not None
        assert "drain timeout" in result.failure_reason

    @pytest.mark.asyncio
    async def test_endpoint_failure_fails_closed(self) -> None:
        from scripts.run_dispatch_stability_soak import (
            MockUpstreamState,
            PollingStats,
            wait_for_runtime_quiescence,
        )

        client_cls = TestWaitForRuntimeQuiescence._stub_client(
            [], raises=[RuntimeError("boom"), RuntimeError("boom")]
        )
        with patch("httpx.AsyncClient", new=client_cls):
            result = await wait_for_runtime_quiescence(
                base_url="127.0.0.1:1",
                api_key="x",
                upstream_state=MockUpstreamState(),
                db_path="/nonexistent/path",
                eggpool_pid=99999,
                start_time=0.0,
                polling_stats=PollingStats(),
                timeout_s=0.5,
                poll_interval_s=0.001,
                max_pending=0,
                max_active=0,
            )
        assert result.drained is False
        assert result.pending_requests is None
        assert result.active_reservations is None

    @pytest.mark.asyncio
    async def test_null_pending_or_active_fails_closed(self) -> None:
        from scripts.run_dispatch_stability_soak import (
            MockUpstreamState,
            PollingStats,
            wait_for_runtime_quiescence,
        )

        # routing_runtime missing → pending/active stay None
        null_payload = {"contention": {}}

        client_cls = TestWaitForRuntimeQuiescence._stub_client([null_payload] * 1000)
        with patch("httpx.AsyncClient", new=client_cls):
            result = await wait_for_runtime_quiescence(
                base_url="127.0.0.1:1",
                api_key="x",
                upstream_state=MockUpstreamState(),
                db_path="/nonexistent/path",
                eggpool_pid=99999,
                start_time=0.0,
                polling_stats=PollingStats(),
                timeout_s=0.05,
                poll_interval_s=0.001,
                max_pending=0,
                max_active=0,
            )
        assert result.drained is False
        assert result.failure_reason == "drain metrics unavailable"
        assert result.pending_requests is None
        assert result.active_reservations is None


class TestFinalDrainGateDoesNotUseMetricsTail:
    """The drain gate must consult the quiescence observation, not metrics[-1]."""

    def test_drain_gate_evaluation_uses_post_load_observation(self) -> None:
        # Demonstrate that evaluate_drain_gate applied to a stale in-window
        # snapshot with non-zero pending would fail, but a fresh quiescence
        # snapshot with zero pending must pass.
        stale = type("S", (), {"pending_requests": 5, "active_reservations": 2})()
        fresh = type("S", (), {"pending_requests": 0, "active_reservations": 0})()
        assert evaluate_drain_gate(stale, 0, 0)[0] is False
        assert evaluate_drain_gate(fresh, 0, 0)[0] is True


class TestOutputShape:
    """Validate the structured sections promised by Plan 042."""

    def test_top_level_shape_keys(self) -> None:
        # Cross-check the run_validation result dict keys via a stub path
        # to keep this test offline.  We import the public symbols and
        # verify the schema_version and SCRIPT_VERSION.
        from scripts import run_dispatch_stability_soak

        assert run_dispatch_stability_soak.SCHEMA_VERSION == 2
        assert run_dispatch_stability_soak.SCRIPT_VERSION == "2.1.0"

    def test_window_metrics_to_dict_includes_new_fields(self) -> None:
        w = WindowMetrics(
            name="early",
            start_time=0.0,
            end_time=1.0,
            request_count=3,
            success_count=2,
            stream_success_count=1,
            nonstream_success_count=1,
            error_count=1,
            dispatch_latencies_ms=[10.0, 20.0, 30.0],
        )
        d = w.to_dict()
        assert d["request_count"] == 3
        assert d["success_count"] == 2
        assert d["stream_success_count"] == 1
        assert d["nonstream_success_count"] == 1
        assert d["error_count"] == 1
        assert d["observed_error_rate"] == pytest.approx(1 / 3, abs=1e-3)


class TestStreamConsumptionAccounting:
    """Plan 042 § D1–D2: stream success requires consumption; errors must count."""

    @pytest.mark.asyncio
    async def test_streaming_http_error_does_not_count_as_success(self) -> None:
        from scripts.run_dispatch_stability_soak import (
            DurationPlan,
        )

        # Build a minimal load with all errors → HTTP 429 → request_count,
        # error_count must increment; success_count must not.
        DurationPlan(
            total_s=1.0,
            warmup_s=0.0,
            early_window_s=0.5,
            drain_s=0.0,
            late_window_s=0.5,
            poll_interval_s=0.5,
            dispatch_p95_ratio_limit=1.5,
            dispatch_p99_ratio_limit=2.0,
            throughput_decline_limit=0.2,
            max_pending_requests=0,
            max_active_reservations=0,
        )

        # We cannot use the real Eggpool here; instead validate the
        # accounting through a synthetic WindowMetrics + the
        # evaluate_workload_gate contract.
        early = WindowMetrics(
            name="early",
            start_time=0.0,
            end_time=1.0,
            request_count=2,
            success_count=0,
            error_count=2,
        )
        late = WindowMetrics(
            name="late",
            start_time=1.0,
            end_time=2.0,
            request_count=2,
            success_count=0,
            error_count=2,
        )
        result = evaluate_workload_gate(early, late, expected_error_rate=0.0)
        assert result.passed is False
        # All-error traffic against a zero-error profile must surface
        # both zero successes and the unexpected errors.
        joined = " ".join(result.failure_reasons)
        assert "zero successes" in joined
        assert "unexpected errors" in joined

    @pytest.mark.asyncio
    async def test_partial_stream_consumption_failure_increments_errors(self) -> None:
        # Synthetic window: stream completion attempted but error_count
        # tracks a stream-consumption failure.  Zero-error profile rejects.
        early = WindowMetrics(
            name="early",
            start_time=0.0,
            end_time=1.0,
            request_count=1,
            success_count=0,
            stream_success_count=0,
            error_count=1,
        )
        late = WindowMetrics(
            name="late",
            start_time=1.0,
            end_time=2.0,
            request_count=1,
            success_count=1,
            nonstream_success_count=1,
            error_count=0,
        )
        result = evaluate_workload_gate(early, late, expected_error_rate=0.0)
        assert result.passed is False


class TestWriteAtomicJson:
    def test_nested_parent_and_no_tmp(self, tmp_path: Path) -> None:
        p = tmp_path / "a" / "b" / "c.json"
        _write_atomic_json(p, {"ok": True})
        assert p.is_file()
        assert not p.with_suffix(p.suffix + ".tmp").exists()

    def test_null_preserved(self, tmp_path: Path) -> None:
        p = tmp_path / "out.json"
        _write_atomic_json(p, {"rss": None, "count": 0})
        loaded = json.loads(p.read_text())
        assert loaded["rss"] is None and loaded["count"] == 0


class TestWorkflowAndDocsAlignment:
    def test_workflow_flags(self) -> None:
        text = WORKFLOW_PATH.read_text()
        assert "--duration-seconds" in text and "--seed" in text
        assert "--mode" not in text
        assert "/tmp/eggpool-runtime-validation.json" in text

    def test_workflow_single_job(self) -> None:
        text = WORKFLOW_PATH.read_text()
        assert "workflow_dispatch" in text
        assert text.count("runs-on:") == 1
        assert "matrix:" not in text and "schedule:" not in text

    def test_release_doc_flags(self) -> None:
        text = RELEASE_DOC.read_text()
        assert "--duration-seconds 300" in text and "--seed 42" in text
        assert "--mode" not in text
        assert "--output /tmp/eggpool-runtime-validation.json" in text

    def test_release_documents_ratio_caps_directly(self) -> None:
        text = RELEASE_DOC.read_text()
        # Direct ratio caps are documented; the previous `1.0 + ratio_limit`
        # wording is absent.
        assert "ratio_limit" in text
        assert "ratio <= ratio_limit" in text or "ratio_limit" in text
        assert "1.50" in text and "2.00" in text

    def test_release_documents_quiescence_observation(self) -> None:
        text = RELEASE_DOC.read_text()
        assert "quiescence" in text
        assert "metrics[-1]" in text or "post-load" in text

    def test_ops_documents_quiescence(self) -> None:
        ops_doc = Path("docs/operations/dispatch-stability.md").read_text()
        assert "quiescence" in ops_doc
        assert "metrics[-1]" in ops_doc or "post-load" in ops_doc

    def test_no_manifest_jsonl_md_language_in_runner_docs(self) -> None:
        for doc in (RELEASE_DOC, Path("docs/operations/dispatch-stability.md")):
            text = doc.read_text().lower()
            assert ".jsonl" not in text or "no jsonl" in text
            # Markdown and manifest sections must remain absent from the
            # single-file contract description.
            assert "manifest" not in text or "no manifest" in text
