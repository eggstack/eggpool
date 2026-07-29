from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from scripts.run_dispatch_stability_soak import (
    SCHEMA_VERSION,
    WindowMetrics,
    _build_parser,
    _cleanup_run_artifacts,
    _memory_total_bytes,
    _optional_int,
    _optional_number,
    _pid_is_alive,
    _populate_cleanup_diagnostics,
    _read_process_log_tail,
    _write_atomic_json,
    build_duration_plan,
    build_run_config,
    evaluate_ratio_gate,
    evaluate_workload_gate,
    read_process_rss_bytes,
)

if TYPE_CHECKING:
    from typing import Any

WORKFLOW_PATH = Path(".github/workflows/extended-soak.yml")
RELEASE_DOC = Path("docs/releasing.md")
OPS_DOC = Path("docs/operations/dispatch-stability.md")


# ---------------------------------------------------------------------------
# Duration plan
# ---------------------------------------------------------------------------


def test_build_duration_plan_phase_properties() -> None:
    """Phases stay positive, windows equal, totals track input."""
    for total in [30, 300, 3600]:
        p = build_duration_plan(total)
        assert p.warmup_s > 0 and p.drain_s > 0
        assert p.early_window_s > 0 and p.late_window_s > 0
        assert p.early_window_s == pytest.approx(p.late_window_s, abs=0.01)
        phase_sum = p.warmup_s + p.drain_s + p.early_window_s + p.late_window_s
        assert phase_sum == pytest.approx(total, abs=2.0)


# ---------------------------------------------------------------------------
# Parser / CLI
# ---------------------------------------------------------------------------


def test_build_parser_full_args_and_rejects_invalid() -> None:
    """Parser accepts the supported option set and rejects bad values."""
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

    for bad in [
        ["--mode", "smoke"],
        ["--duration-seconds", "abc"],
        ["--duration-seconds", "0"],
        ["--duration-seconds", "-5"],
        ["--duration-seconds", "29"],
    ]:
        with pytest.raises(SystemExit):
            _build_parser().parse_args(bad)


def test_build_run_config_and_unknown_profile() -> None:
    """``build_run_config`` parses CLI args; unknown profile short-circuits cleanly."""
    parsed = _build_parser().parse_args(
        [
            "--profile",
            "sbc-reference",
            "--duration-seconds",
            "120",
            "--output",
            "/tmp/example.json",
            "--seed",
            "99",
        ]
    )
    config = build_run_config(parsed)
    assert config.profile_name == "sbc-reference"
    assert config.duration_seconds == 120
    assert config.output_path == Path("/tmp/example.json")
    assert config.seed == 99

    async def _run_unknown() -> Any:
        from scripts.run_dispatch_stability_soak import (
            ValidationRunConfig,
            run_validation,
        )

        return await run_validation(
            ValidationRunConfig(
                profile_name="does-not-exist",
                duration_seconds=30,
                output_path=Path("/tmp/should-not-exist.json"),
                seed=42,
            )
        )

    result = asyncio.run(_run_unknown())
    assert result.return_code == 1
    assert result.passed is False
    assert any("unknown profile" in r for r in result.failure_reasons)


# ---------------------------------------------------------------------------
# Resource / metric helpers
# ---------------------------------------------------------------------------


def test_process_rss_bytes_table() -> None:
    """KiB is converted to bytes, malformed/zero values return None."""
    linux_status = "VmRSS:     12345 kB\n"
    with (
        patch("builtins.open", create=True) as m,
        patch.object(sys, "platform", "linux"),
    ):
        m.return_value.__enter__ = lambda s: s
        m.return_value.__exit__ = lambda s, *a: False
        m.return_value.__iter__ = lambda s: iter(linux_status.splitlines())
        assert read_process_rss_bytes(1) == 12345 * 1024

    for lines in [
        ["Name: eggpool", "State: S"],
        ["VmRSS:     12345 bytes"],
        ["VmRSS:     0 kB"],
    ]:
        status = "\n".join(lines) + "\n"
        with (
            patch("builtins.open", create=True) as m,
            patch.object(sys, "platform", "linux"),
        ):
            m.return_value.__enter__ = lambda s: s
            m.return_value.__exit__ = lambda s, *a: False
            m.return_value.__iter__ = lambda s, st=status: iter(st.splitlines())
            assert read_process_rss_bytes(1) is None

    ok = type("R", (), {"returncode": 0, "stdout": "  12345\n"})()
    with (
        patch("subprocess.run", return_value=ok),
        patch.object(sys, "platform", "darwin"),
    ):
        assert read_process_rss_bytes(1) == 12345 * 1024
    fail = type("R", (), {"returncode": 1, "stdout": ""})()
    with (
        patch("subprocess.run", return_value=fail),
        patch.object(sys, "platform", "darwin"),
    ):
        assert read_process_rss_bytes(1) is None
    with (
        patch("subprocess.run", side_effect=OSError("not supported")),
        patch.object(sys, "platform", "win32"),
    ):
        assert read_process_rss_bytes(1) is None


def test_memory_total_bytes_returns_none_on_failure() -> None:
    """sysconf absence / failure yields None; never zero."""
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


def test_optional_number_and_int_filters_booleans() -> None:
    """Booleans are rejected; numeric values are preserved."""
    assert _optional_number(1.5) == 1.5
    assert _optional_number(2) == 2.0
    assert _optional_number(False) is None
    assert _optional_number("1.5") is None

    assert _optional_int(3) == 3
    assert _optional_int(3.0) == 3
    assert _optional_int(3.7) is None
    assert _optional_int(True) is None
    assert _optional_int("3") is None


def test_pid_is_alive_table() -> None:
    """PermissionError reports alive; missing PID reports gone."""
    with patch("os.kill", side_effect=ProcessLookupError):
        assert _pid_is_alive(99999) is False
    with patch("os.kill", side_effect=PermissionError):
        assert _pid_is_alive(1) is True
    with patch("os.kill", side_effect=OSError("other")):
        assert _pid_is_alive(1) is False
    assert _pid_is_alive(0) is False


# ---------------------------------------------------------------------------
# Tiny conversion helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pure gate evaluators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "early, late, limit, expected_pass, expected_reason_substring",
    [
        (10.0, 14.9, 1.50, True, None),
        (10.0, 15.0, 1.50, True, None),
        (10.0, 15.1, 1.50, False, "exceeds limit"),
        (None, 12.4, 1.50, False, "samples unavailable"),
        (10.0, None, 1.50, False, "samples unavailable"),
        (0.0, 12.4, 1.50, False, "non-positive"),
        (-1.0, 12.4, 1.50, False, "non-positive"),
    ],
)
def test_evaluate_ratio_gate_boundary_table(
    early: float | None,
    late: float | None,
    limit: float,
    expected_pass: bool,
    expected_reason_substring: str | None,
) -> None:
    """Direct ratio caps with fail-closed boundary coverage."""
    if early is not None and isinstance(early, float) and early >= 0:
        # rebuild as float to satisfy type checker
        early = float(early)
    result = evaluate_ratio_gate(
        early,  # type: ignore[arg-type]
        late,  # type: ignore[arg-type]
        limit=limit,
        label="dispatch_p95",
    )
    assert result.passed is expected_pass
    if expected_pass:
        assert result.failure_reason is None
        assert result.ratio is not None
        assert result.ratio <= result.limit
    else:
        assert result.failure_reason is not None
        if expected_reason_substring is not None:
            assert expected_reason_substring in result.failure_reason


def test_evaluate_ratio_gate_does_not_add_one_to_limit() -> None:
    """Boundary check: ratio cap is a direct cap, not additive."""
    result = evaluate_ratio_gate(10.0, 14.99, limit=1.50, label="dispatch_p99")
    assert result.passed is True
    assert result.ratio is not None and result.ratio <= result.limit


def _make_window(
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


@pytest.mark.parametrize(
    "case, early_kwargs, late_kwargs, expected_error_rate, require_both, "
    "expected_pass, expected_substring",
    [
        (
            "zero_attempts_fails",
            {"attempts": 0, "successes": 0, "errors": 0},
            {},
            0.0,
            False,
            False,
            "zero attempts",
        ),
        (
            "all_errors_fail_zero_error_profile",
            {
                "attempts": 5,
                "successes": 0,
                "stream_successes": 0,
                "nonstream_successes": 0,
                "errors": 5,
            },
            {
                "attempts": 5,
                "successes": 0,
                "stream_successes": 0,
                "nonstream_successes": 0,
                "errors": 5,
            },
            0.0,
            False,
            False,
            "zero successes",
        ),
        (
            "one_success_per_window_passes_minimum",
            {"attempts": 1, "successes": 1, "errors": 0},
            {"attempts": 1, "successes": 1, "errors": 0},
            0.0,
            False,
            True,
            None,
        ),
        (
            "configured_error_rate_tolerates_some",
            {"attempts": 10, "successes": 9, "errors": 1},
            {"attempts": 10, "successes": 9, "errors": 1},
            0.05,
            False,
            True,
            None,
        ),
        (
            "configured_error_rate_rejects_excess",
            {"attempts": 10, "successes": 4, "errors": 6},
            {"attempts": 10, "successes": 5, "errors": 5},
            0.05,
            False,
            False,
            "error rate",
        ),
        (
            "stream_and_nonstream_required",
            {
                "attempts": 2,
                "successes": 2,
                "stream_successes": 2,
                "nonstream_successes": 0,
            },
            {
                "attempts": 2,
                "successes": 2,
                "stream_successes": 0,
                "nonstream_successes": 2,
            },
            0.0,
            True,
            True,
            None,
        ),
        (
            "stream_and_nonstream_missing_one",
            {
                "attempts": 2,
                "successes": 2,
                "stream_successes": 2,
                "nonstream_successes": 0,
            },
            {
                "attempts": 2,
                "successes": 2,
                "stream_successes": 2,
                "nonstream_successes": 0,
            },
            0.0,
            True,
            False,
            "non-streaming",
        ),
    ],
)
def test_evaluate_workload_gate_table(
    case: str,
    early_kwargs: dict[str, int],
    late_kwargs: dict[str, int],
    expected_error_rate: float,
    require_both: bool,
    expected_pass: bool,
    expected_substring: str | None,
) -> None:
    """Per-window attempts, successes, errors, and dual-shape coverage."""
    early = _make_window(**early_kwargs)
    late = _make_window(**late_kwargs)
    result = evaluate_workload_gate(
        early,
        late,
        expected_error_rate=expected_error_rate,
        require_stream_and_nonstream=require_both,
    )
    assert result.passed is expected_pass, case
    if expected_substring is not None:
        joined = " ".join(result.failure_reasons)
        assert expected_substring in joined, case


def test_evaluate_workload_gate_stream_failure_increments_errors() -> None:
    """Stream-consumption failure increments errors; zero-error profile rejects."""
    early = WindowMetrics(
        name="early",
        start_time=0.0,
        end_time=1.0,
        request_count=1,
        success_count=0,
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


# ---------------------------------------------------------------------------
# Schema + output shape
# ---------------------------------------------------------------------------


def test_schema_version_and_window_metrics_shape() -> None:
    """Schema is v2; WindowMetrics includes all new accounting fields."""
    assert SCHEMA_VERSION >= 2
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


def test_write_atomic_json_nested_path_and_null_preservation(
    tmp_path: Path,
) -> None:
    """Atomic writes create parent dirs and preserve None values."""
    p = tmp_path / "a" / "b" / "c.json"
    _write_atomic_json(p, {"ok": True})
    assert p.is_file()
    assert not p.with_suffix(p.suffix + ".tmp").exists()

    p2 = tmp_path / "out.json"
    _write_atomic_json(p2, {"rss": None, "count": 0})
    loaded = json.loads(p2.read_text())
    assert loaded["rss"] is None and loaded["count"] == 0


# ---------------------------------------------------------------------------
# Runtime snapshot parsing — must read the real db.contention shape
# ---------------------------------------------------------------------------


def _stub_client(payload: dict[str, Any] | None) -> Any:
    class _Resp:
        def __init__(self, inner: dict[str, Any] | None) -> None:
            self._inner = inner
            self.status_code = 200 if inner is not None else 503

        def json(self) -> dict[str, Any]:
            if self._inner is None:
                raise RuntimeError("no payload")
            return self._inner

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG004
            pass

        async def get(self, url: str, headers: dict[str, str]) -> Any:  # noqa: ARG002
            return _Resp(payload)

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    return _Client()


@pytest.mark.asyncio
async def test_collect_snapshot_reads_real_db_contention_shape() -> None:
    """Runner must parse contention from ``db.contention`` (real endpoint shape)."""
    from scripts.run_dispatch_stability_soak import (
        MockUpstreamState,
        PollingStats,
        collect_runtime_snapshot,
    )

    payload = {
        "db": {
            "contention": {
                "lock_wait_p95_ms": 0.5,
                "lock_wait_max_ms": 1.5,
                "lock_wait_sample_count": 10,
            }
        },
        "routing_runtime": {
            "pending_count": 0,
            "active_reservations_count": 0,
        },
    }
    snap = await collect_runtime_snapshot(
        client=_stub_client(payload),
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
    assert snap.db_lock_wait_max_ms == 1.5
    assert snap.db_lock_wait_sample_count == 10


@pytest.mark.parametrize(
    "case, payload",
    [
        ("missing_db", {"routing_runtime": {}}),
        ("db_none", None),
        ("missing_contention", {"db": {}}),
        ("contention_none", {"db": {"contention": None}}),
        ("malformed_contention_scalar", {"db": {"contention": "nope"}}),
        (
            "malformed_individual_values",
            {"db": {"contention": {"lock_wait_p95_ms": "x"}}},
        ),
    ],
)
@pytest.mark.asyncio
async def test_collect_snapshot_malformed_contention_table(
    case: str,
    payload: Any,
) -> None:
    """Missing or malformed contention leaves metrics at ``None`` — never zero."""
    from scripts.run_dispatch_stability_soak import (
        MockUpstreamState,
        PollingStats,
        collect_runtime_snapshot,
    )

    transport_payload: dict[str, Any] = payload if isinstance(payload, dict) else {}
    if "routing_runtime" not in transport_payload:
        transport_payload["routing_runtime"] = {
            "pending_count": 0,
            "active_reservations_count": 0,
        }

    class _Resp:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return transport_payload

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG004
            pass

        async def get(self, url: str, headers: dict[str, str]) -> Any:  # noqa: ARG002
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
    assert snap.db_lock_wait_p95_ms is None, case
    assert snap.db_lock_wait_max_ms is None, case
    assert snap.db_lock_wait_sample_count is None, case


@pytest.mark.asyncio
async def test_collect_snapshot_unavailable_on_failure() -> None:
    """Runtime endpoint failure leaves all derived fields ``None``."""
    from scripts.run_dispatch_stability_soak import (
        MockUpstreamState,
        PollingStats,
        collect_runtime_snapshot,
    )

    class _RaisingClient:
        async def get(self, url: str, headers: dict[str, str]) -> None:  # noqa: ARG002
            raise RuntimeError("boom")

        async def __aenter__(self) -> _RaisingClient:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    snap = await collect_runtime_snapshot(
        client=_RaisingClient(),
        api_key="x",
        upstream_state=MockUpstreamState(),
        db_path="/nonexistent/path",
        eggpool_pid=99999,
        start_time=0.0,
        polling_stats=PollingStats(),
        include_summary=False,
    )
    assert snap.pending_requests is None
    assert snap.active_reservations is None
    assert snap.upstream_requests == 0


@pytest.mark.asyncio
async def test_collect_snapshot_non_200_leaves_derived_fields_none() -> None:
    """Non-200 runtime response leaves derived fields at ``None``."""
    from scripts.run_dispatch_stability_soak import (
        MockUpstreamState,
        PollingStats,
        collect_runtime_snapshot,
    )

    class _Resp:
        status_code = 500

        def json(self) -> dict[str, Any]:
            return {}

    class _Client:
        async def get(self, url: str, headers: dict[str, str]) -> Any:  # noqa: ARG002
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
    assert snap.pending_requests is None
    assert snap.active_reservations is None


# ---------------------------------------------------------------------------
# Bounded quiescence polling
# ---------------------------------------------------------------------------


def _quiescence_client(
    runtime_payloads: list[dict[str, Any] | None],
    raises: list[Exception] | None = None,
) -> Any:
    """Mock httpx.AsyncClient that returns summary then iterates runtime payloads."""

    class _Resp:
        def __init__(self, inner: dict[str, Any] | None) -> None:
            self._inner = inner
            self.status_code = 200 if inner is not None else 500

        def json(self) -> dict[str, Any]:
            if self._inner is None:
                raise RuntimeError("no payload")
            return self._inner

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG004
            self._idx = 0
            self._runtime = list(runtime_payloads)
            self._raises = list(raises or [])
            self._call_count = 0

        async def get(self, url: str, headers: dict[str, str]) -> Any:  # noqa: ARG002
            self._call_count += 1
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


def _drained_payload(pending: int = 0, active: int = 0) -> dict[str, Any]:
    return {
        "db": {"contention": {}},
        "routing_runtime": {
            "pending_count": pending,
            "active_reservations_count": active,
        },
    }


@pytest.mark.asyncio
async def test_quiescence_outcomes_table() -> None:
    """Quiescence returns fail-closed semantics across the scenario table."""
    from scripts.run_dispatch_stability_soak import (
        MockUpstreamState,
        PollingStats,
        wait_for_runtime_quiescence,
    )

    async def _run(payloads: list[dict[str, Any] | None]) -> Any:
        client_cls = _quiescence_client(payloads)
        with patch("httpx.AsyncClient", new=client_cls):
            return await wait_for_runtime_quiescence(
                base_url="127.0.0.1:1",
                api_key="x",
                upstream_state=MockUpstreamState(),
                db_path="/nonexistent/path",
                eggpool_pid=99999,
                start_time=0.0,
                polling_stats=PollingStats(),
                timeout_s=2.0,
                poll_interval_s=0.001,
                max_pending=0,
                max_active=0,
            )

    # First observation already drained.
    r1 = await _run([_drained_payload()])
    assert r1.drained is True
    assert r1.attempts == 1
    assert r1.failure_reason is None

    # Active first, then drained.
    r2 = await _run([_drained_payload(pending=2, active=1), _drained_payload()])
    assert r2.drained is True
    assert r2.attempts == 2

    # Repeated active state — short timeout forces failure.
    async def _run_active_timeout() -> Any:
        client_cls = _quiescence_client([_drained_payload(pending=5, active=3)] * 1000)
        with patch("httpx.AsyncClient", new=client_cls):
            return await wait_for_runtime_quiescence(
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

    r3 = await _run_active_timeout()
    assert r3.drained is False
    assert r3.failure_reason is not None
    assert "drain timeout" in r3.failure_reason

    # Endpoint failure fails closed.
    async def _run_raises() -> Any:
        client_cls = _quiescence_client(
            [], raises=[RuntimeError("boom"), RuntimeError("boom")]
        )
        with patch("httpx.AsyncClient", new=client_cls):
            return await wait_for_runtime_quiescence(
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

    r4 = await _run_raises()
    assert r4.drained is False
    assert r4.pending_requests is None
    assert r4.active_reservations is None

    # Nullable pending/active fails closed.
    async def _run_null() -> Any:
        client_cls = _quiescence_client([{"db": {"contention": {}}}] * 1000)
        with patch("httpx.AsyncClient", new=client_cls):
            return await wait_for_runtime_quiescence(
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

    r5 = await _run_null()
    assert r5.drained is False
    assert r5.failure_reason == "drain metrics unavailable"
    assert r5.pending_requests is None
    assert r5.active_reservations is None


# ---------------------------------------------------------------------------
# Cleanup diagnostics
# ---------------------------------------------------------------------------


def _stub_proc(*, already_dead: bool) -> Any:
    class _P:
        def __init__(self) -> None:
            self.pid = 4321
            self._killed = False
            self._dead = already_dead

        def poll(self) -> int | None:
            return 0 if self._dead else None

        def terminate(self) -> None:
            self._killed = True
            self._dead = True

        def wait(self, timeout: float) -> None:
            self._dead = True

    return _P()


def test_populate_cleanup_diagnostics_records_success_and_failure() -> None:
    """Cleanup populates diagnostics; failures are surfaced, not swallowed."""
    work_dir = Path(tempfile.mkdtemp(prefix="eggpool-test-"))
    proc = _stub_proc(already_dead=False)
    log_path = work_dir / "process.log"
    log_path.write_text("hello world\n", encoding="utf-8")

    result = ValidationResultShim()
    _populate_cleanup_diagnostics(
        result,
        proc=proc,
        process_log_path=log_path,
        work_dir=work_dir,
        upstream_server=None,
    )
    assert result.child_pid == 4321
    assert result.process_stopped is True
    assert result.work_dir_removed is True
    assert result.cleanup_error is None
    assert "hello world" in result.process_log_tail

    # Failure path: rmtree fails — error captured, work_dir still listed as not removed.
    bad_dir = Path(tempfile.mkdtemp(prefix="eggpool-bad-"))
    (bad_dir / "file.txt").write_text("x", encoding="utf-8")
    # Make rmtree fail by pointing at a path that is itself a file.
    blocking_file = bad_dir / "file.txt"
    bad_result = ValidationResultShim()
    _populate_cleanup_diagnostics(
        bad_result,
        proc=_stub_proc(already_dead=False),
        process_log_path=log_path,
        work_dir=blocking_file,
        upstream_server=None,
    )
    assert bad_result.cleanup_error is not None
    assert bad_result.work_dir_removed is False

    # No work to do for None proc.
    none_result = ValidationResultShim()
    _populate_cleanup_diagnostics(
        none_result,
        proc=None,
        process_log_path=log_path,
        work_dir=work_dir,
        upstream_server=None,
    )
    assert none_result.child_pid is None
    assert none_result.process_stopped is None


def test_read_process_log_tail_bounds_and_redacts() -> None:
    """Log tail is bounded, redacted, and survives missing files."""
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as tmp:
        tmp.write("Authorization: Bearer sk-supersecret123\n" * 200)
    log_path = Path(tmp.name)
    try:
        tail = _read_process_log_tail(log_path, max_lines=10)
        assert "supersecret" not in tail  # redacted via credential pattern
        assert len(tail.splitlines()) <= 10
    finally:
        log_path.unlink()

    assert _read_process_log_tail(Path("/nonexistent/path/log")) == ""


def test_cleanup_run_artifacts_captures_exception_path() -> None:
    """Cleanup returns the cleanup_error string when rmtree raises."""
    work_dir = Path(tempfile.mkdtemp(prefix="eggpool-art-"))
    (work_dir / "x").write_text("x", encoding="utf-8")
    with patch(
        "scripts.run_dispatch_stability_soak.shutil.rmtree",
        side_effect=OSError("disk full"),
    ):
        log_tail, process_stopped, work_dir_removed, cleanup_error = (
            _cleanup_run_artifacts(
                proc=_stub_proc(already_dead=True),
                process_log_path=work_dir / "process.log",
                work_dir=work_dir,
                upstream_server=None,
            )
        )
    assert cleanup_error is not None
    assert "disk full" in cleanup_error
    assert work_dir_removed is False
    assert process_stopped is True


# ---------------------------------------------------------------------------
# Workflow + documentation alignment
# ---------------------------------------------------------------------------


def test_workflow_and_documentation_alignment() -> None:
    """Workflow and docs agree with the supported runner contract."""
    workflow = WORKFLOW_PATH.read_text()
    assert "workflow_dispatch" in workflow
    assert workflow.count("runs-on:") == 1
    assert "matrix:" not in workflow and "schedule:" not in workflow
    assert "--duration-seconds" in workflow and "--seed" in workflow
    assert "--mode" not in workflow
    assert "/tmp/eggpool-runtime-validation.json" in workflow

    release = RELEASE_DOC.read_text()
    assert "--duration-seconds 300" in release and "--seed 42" in release
    assert "--mode" not in release
    assert "--output /tmp/eggpool-runtime-validation.json" in release
    assert "ratio <= ratio_limit" in release
    assert "1.50" in release and "2.00" in release
    assert "quiescence" in release
    assert "post-load" in release or "metrics[-1]" in release

    ops = OPS_DOC.read_text()
    assert "quiescence" in ops
    assert "post-load" in ops or "metrics[-1]" in ops

    for doc in (release_doc_lower(RELEASE_DOC), release_doc_lower(OPS_DOC)):
        assert ".jsonl" not in doc or "no jsonl" in doc
        assert "manifest" not in doc or "no manifest" in doc


def release_doc_lower(path: Path) -> str:
    return path.read_text().lower()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class ValidationResultShim:
    """Lightweight stand-in for ``ValidationResult`` exposing cleanup fields."""

    def __init__(self) -> None:
        self.passed = False
        self.failure_reasons: list[str] = []
        self.output_path = Path("/tmp/should-not-exist.json")
        self.duration_s = 0.0
        self.return_code = 1
        self.child_pid: int | None = None
        self.work_dir: Path | None = None
        self.process_log_tail = ""
        self.process_stopped: bool | None = None
        self.work_dir_removed: bool | None = None
        self.cleanup_error: str | None = None


# Imported late so pytest always collects the module-level tests.
import tempfile  # noqa: E402
