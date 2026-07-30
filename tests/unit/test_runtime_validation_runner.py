from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from scripts.run_dispatch_stability_soak import (
    SCHEMA_VERSION,
    DurationPlan,
    WindowMetrics,
    _build_parser,
    _cleanup_run_artifacts,
    _memory_total_bytes,
    _optional_int,
    _optional_number,
    _pid_is_alive,
    _populate_cleanup_diagnostics,
    _read_process_log_tail,
    _redact_log_line,
    _write_atomic_json,
    build_duration_plan,
    build_run_config,
    evaluate_ratio_gate,
    evaluate_workload_gate,
    read_process_rss_bytes,
)

if TYPE_CHECKING:
    from typing import Any

    from scripts.run_dispatch_stability_soak import ValidationResult

WORKFLOW_PATH = Path(".github/workflows/extended-soak.yml")
RELEASE_DOC = Path("docs/releasing.md")
OPS_DOC = Path("docs/operations/dispatch-stability.md")


# ---------------------------------------------------------------------------
# Duration plan
# ---------------------------------------------------------------------------


def test_build_duration_plan_and_run_config() -> None:
    for total in [30, 300, 3600]:
        p = build_duration_plan(total)
        assert p.warmup_s > 0 and p.drain_s > 0
        assert p.early_window_s > 0 and p.late_window_s > 0
        assert p.early_window_s == pytest.approx(p.late_window_s, abs=0.01)
        phase_sum = p.warmup_s + p.drain_s + p.early_window_s + p.late_window_s
        assert phase_sum == pytest.approx(total, abs=2.0)

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


def test_optional_number_and_int_filters_booleans() -> None:
    assert _optional_number(1.5) == 1.5
    assert _optional_number(2) == 2.0
    assert _optional_number(False) is None
    assert _optional_number("1.5") is None

    assert _optional_int(3) == 3
    assert _optional_int(3.0) == 3
    assert _optional_int(3.7) is None
    assert _optional_int(True) is None
    assert _optional_int("3") is None

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


def test_evaluate_ratio_gate_boundary_table() -> None:
    cases = [
        (10.0, 14.9, 1.50, True, None),
        (10.0, 15.0, 1.50, True, None),
        (10.0, 15.1, 1.50, False, "exceeds limit"),
        (None, 12.4, 1.50, False, "samples unavailable"),
        (10.0, None, 1.50, False, "samples unavailable"),
        (0.0, 12.4, 1.50, False, "non-positive"),
        (-1.0, 12.4, 1.50, False, "non-positive"),
        (10.0, 14.99, 1.50, True, None),
    ]
    for early, late, limit, expected_pass, expected_reason_sub in cases:
        result = evaluate_ratio_gate(
            early,
            late,
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
            if expected_reason_sub is not None:
                assert expected_reason_sub in result.failure_reason


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


def test_evaluate_workload_gate_table() -> None:
    cases = [
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
        (
            "stream_failure_early_zero_error_profile",
            {"attempts": 1, "successes": 0, "errors": 1},
            {"attempts": 1, "successes": 1, "nonstream_successes": 1, "errors": 0},
            0.0,
            False,
            False,
            "zero successes",
        ),
    ]
    for (
        case,
        early_kwargs,
        late_kwargs,
        expected_error_rate,
        require_both,
        expected_pass,
        expected_sub,
    ) in cases:
        early = _make_window(**early_kwargs)
        late = _make_window(**late_kwargs)
        result = evaluate_workload_gate(
            early,
            late,
            expected_error_rate=expected_error_rate,
            require_stream_and_nonstream=require_both,
        )
        assert result.passed is expected_pass, case
        if expected_sub is not None:
            joined = " ".join(result.failure_reasons)
            assert expected_sub in joined, case


# ---------------------------------------------------------------------------
# Schema + output shape
# ---------------------------------------------------------------------------


def test_schema_window_metrics_and_atomic_json() -> None:
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

    p = Path(tempfile.mkdtemp(prefix="eggpool-json-")) / "a" / "b" / "c.json"
    _write_atomic_json(p, {"ok": True})
    assert p.is_file()
    assert not p.with_suffix(p.suffix + ".tmp").exists()

    p2 = Path(tempfile.mkdtemp(prefix="eggpool-json-")) / "out.json"
    _write_atomic_json(p2, {"rss": None, "count": 0})
    loaded = json.loads(p2.read_text())
    assert loaded["rss"] is None and loaded["count"] == 0
    shutil.rmtree(p.parent.parent, ignore_errors=True)
    shutil.rmtree(p2.parent, ignore_errors=True)


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


@pytest.mark.asyncio
async def test_collect_snapshot_malformed_contention_table() -> None:
    from scripts.run_dispatch_stability_soak import (
        MockUpstreamState,
        PollingStats,
        collect_runtime_snapshot,
    )

    cases = [
        ("missing_db", {"routing_runtime": {}}),
        ("db_none", {"db": None, "routing_runtime": {}}),
        ("missing_contention", {"db": {}}),
        ("contention_none", {"db": {"contention": None}}),
        ("malformed_contention_scalar", {"db": {"contention": "nope"}}),
        (
            "malformed_individual_values",
            {"db": {"contention": {"lock_wait_p95_ms": "x"}}},
        ),
    ]

    for case, payload in cases:
        transport_payload: dict[str, Any] = payload if isinstance(payload, dict) else {}
        if "routing_runtime" not in transport_payload:
            transport_payload["routing_runtime"] = {
                "pending_count": 0,
                "active_reservations_count": 0,
            }

        class _Resp:
            status_code = 200

            def __init__(self, payload: dict[str, Any]) -> None:
                self._payload = payload

            def json(self) -> dict[str, Any]:
                return self._payload

        class _Client:
            def __init__(self, payload: dict[str, Any]) -> None:
                self._payload = payload

            async def get(self, url: str, headers: dict[str, str]) -> Any:  # noqa: ARG002
                return _Resp(self._payload)

            async def __aenter__(self) -> _Client:
                return self

            async def __aexit__(self, *exc: Any) -> None:
                return None

        snap = await collect_runtime_snapshot(
            client=_Client(transport_payload),
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
async def test_collect_snapshot_unavailable_and_non_200() -> None:
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

    class _Non200Resp:
        status_code = 500

        def json(self) -> dict[str, Any]:
            return {}

    class _Non200Client:
        async def get(self, url: str, headers: dict[str, str]) -> Any:  # noqa: ARG002
            return _Non200Resp()

        async def __aenter__(self) -> _Non200Client:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    for client, label in [(_RaisingClient(), "exception"), (_Non200Client(), "500")]:
        ps = PollingStats()
        snap = await collect_runtime_snapshot(
            client=client,
            api_key="x",
            upstream_state=MockUpstreamState(),
            db_path="/nonexistent/path",
            eggpool_pid=99999,
            start_time=0.0,
            polling_stats=ps,
            include_summary=False,
        )
        assert snap.pending_requests is None, label
        assert snap.active_reservations is None, label
        assert snap.upstream_requests == 0, label
        assert ps.runtime_failures == 1, label
        assert ps.runtime_successes == 0, label


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


def test_cleanup_diagnostics_and_artifacts_loop() -> None:
    work_dir = Path(tempfile.mkdtemp(prefix="eggpool-test-"))
    log_path = work_dir / "process.log"
    log_path.write_text("hello world\n", encoding="utf-8")

    proc = _stub_proc(already_dead=False)
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
    assert result.cleanup_errors == ()
    assert "hello world" in result.process_log_tail

    bad_dir = Path(tempfile.mkdtemp(prefix="eggpool-bad-"))
    (bad_dir / "file.txt").write_text("x", encoding="utf-8")
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
    assert len(bad_result.cleanup_errors) > 0

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

    shutil.rmtree(work_dir, ignore_errors=True)
    shutil.rmtree(bad_dir, ignore_errors=True)


def test_read_process_log_tail_bounds_redacts_and_forms() -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as tmp:
        tmp.write("Authorization: Bearer sk-supersecret123\n" * 200)
    log_path = Path(tmp.name)
    try:
        tail = _read_process_log_tail(log_path, max_lines=10)
        assert "supersecret" not in tail
        assert len(tail.splitlines()) <= 10
    finally:
        log_path.unlink()

    assert _read_process_log_tail(Path("/nonexistent/path/log")) == ""

    r1 = _redact_log_line("Authorization: Bearer sk-supersecret123")
    assert "sk-supersecret123" not in r1
    assert "<redacted>" in r1
    r2 = _redact_log_line("api_key=sk-supersecret123")
    assert "sk-supersecret123" not in r2
    r3 = _redact_log_line("api-key=sk-supersecret123")
    assert "sk-supersecret123" not in r3
    assert _redact_log_line("") == ""
    assert _redact_log_line("normal log line") == "normal log line"

    lines = [
        "Authorization: Bearer sk-supersecret123",
        "ERROR eggpool.db: database connection failed",
        "INFO eggpool: shutting down",
    ]
    redacted = [_redact_log_line(ln) for ln in lines]
    assert "sk-supersecret123" not in redacted[0]
    assert "<redacted>" in redacted[0]
    assert "database connection failed" in redacted[1]
    assert "shutting down" in redacted[2]

    content = (
        "Authorization: Bearer sk-supersecret123\n"
        "ERROR eggpool.db: database connection failed\n"
        "INFO eggpool: shutting down\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as tmp2:
        tmp2.write(content)
    log_path2 = Path(tmp2.name)
    try:
        tail2 = _read_process_log_tail(log_path2)
        assert "sk-supersecret123" not in tail2
        assert "database connection failed" in tail2
        assert "shutting down" in tail2
    finally:
        log_path2.unlink()


def test_cleanup_rmtree_exception_and_setup_loop() -> None:
    work_dir = Path(tempfile.mkdtemp(prefix="eggpool-art-"))
    (work_dir / "x").write_text("x", encoding="utf-8")
    with patch(
        "scripts.run_dispatch_stability_soak.shutil.rmtree",
        side_effect=OSError("disk full"),
    ):
        _, process_stopped, work_dir_removed, cleanup_errors = _cleanup_run_artifacts(
            proc=_stub_proc(already_dead=True),
            process_log_path=work_dir / "process.log",
            work_dir=work_dir,
            upstream_server=None,
        )
    assert any("disk full" in e for e in cleanup_errors)
    assert work_dir_removed is False
    assert process_stopped is True
    shutil.rmtree(work_dir, ignore_errors=True)

    setup_dir = Path(tempfile.mkdtemp(prefix="eggpool-setup-"))
    (setup_dir / "x").write_text("x", encoding="utf-8")
    mock_upstream = MagicMock()
    mock_upstream.server_address = ("127.0.0.1", 9999)
    _, process_stopped, work_dir_removed, cleanup_errors = _cleanup_run_artifacts(
        proc=None,
        process_log_path=setup_dir / "process.log",
        work_dir=setup_dir,
        upstream_server=mock_upstream,
    )
    assert process_stopped is None
    assert work_dir_removed is True
    assert cleanup_errors == ()
    mock_upstream.shutdown.assert_called_once()
    mock_upstream.server_close.assert_called_once()
    shutil.rmtree(setup_dir, ignore_errors=True)


def test_cleanup_failure_table_parametrized() -> None:
    def _make_terminate_raises() -> Any:
        p = _stub_proc(already_dead=False)
        p.terminate = MagicMock(side_effect=OSError("kill denied"))  # type: ignore[assignment]
        return p

    def _make_alive_poll() -> Any:
        p = _stub_proc(already_dead=False)
        p.poll = MagicMock(return_value=None)  # type: ignore[assignment]
        return p

    def _make_shutdown_raises() -> Any:
        m = MagicMock(server_address=("127.0.0.1", 9999))
        m.shutdown.side_effect = OSError("shutdown denied")
        return m

    def _make_close_raises() -> Any:
        m = MagicMock(server_address=("127.0.0.1", 9999))
        m.server_close.side_effect = OSError("close denied")
        return m

    cases = [
        (
            "terminate_raises",
            _make_terminate_raises,
            lambda: MagicMock(server_address=("127.0.0.1", 9999)),
            ("child termination failed",),
            False,
        ),
        (
            "process_remains_alive",
            _make_alive_poll,
            lambda: None,
            ("child process remained alive",),
            False,
        ),
        (
            "upstream_shutdown_raises",
            lambda: _stub_proc(already_dead=True),
            _make_shutdown_raises,
            ("mock upstream shutdown failed",),
            True,
        ),
        (
            "upstream_close_raises",
            lambda: _stub_proc(already_dead=True),
            _make_close_raises,
            ("mock upstream close failed",),
            True,
        ),
        (
            "setup_upstream_and_workdir",
            lambda: None,
            lambda: MagicMock(server_address=("127.0.0.1", 9999)),
            (),
            None,
        ),
    ]

    for (
        case,
        proc_factory,
        upstream_factory,
        expected_errors_substrings,
        expected_process_stopped,
    ) in cases:
        work_dir = Path(tempfile.mkdtemp(prefix="eggpool-cln-"))
        (work_dir / "x").write_text("x", encoding="utf-8")
        proc = proc_factory()
        upstream = upstream_factory()
        log_tail, process_stopped, work_dir_removed, cleanup_errors = (
            _cleanup_run_artifacts(
                proc=proc,
                process_log_path=work_dir / "process.log",
                work_dir=work_dir,
                upstream_server=upstream,
            )
        )
        assert process_stopped is expected_process_stopped, case
        for sub in expected_errors_substrings:
            assert any(sub in e for e in cleanup_errors), case
        assert work_dir_removed is True, case
        if upstream is not None:
            upstream.shutdown.assert_called_once()
            upstream.server_close.assert_called_once()
        shutil.rmtree(work_dir, ignore_errors=True)


def test_cleanup_failure_table_multiple_errors_aggregated() -> None:
    work_dir = Path(tempfile.mkdtemp(prefix="eggpool-multi-"))
    (work_dir / "x").write_text("x", encoding="utf-8")
    mock_upstream = MagicMock()
    mock_upstream.server_address = ("127.0.0.1", 9999)
    mock_upstream.shutdown.side_effect = OSError("shutdown denied")
    mock_upstream.server_close.side_effect = OSError("close denied")
    proc = _stub_proc(already_dead=False)
    proc.terminate = MagicMock(side_effect=OSError("kill denied"))  # type: ignore[assignment]
    proc.poll = MagicMock(return_value=None)  # type: ignore[assignment]
    real_rmtree = shutil.rmtree
    with patch(
        "scripts.run_dispatch_stability_soak.shutil.rmtree",
        side_effect=OSError("disk full"),
    ):
        log_tail, process_stopped, work_dir_removed, cleanup_errors = (
            _cleanup_run_artifacts(
                proc=proc,
                process_log_path=work_dir / "process.log",
                work_dir=work_dir,
                upstream_server=mock_upstream,
            )
        )
        assert process_stopped is False
        assert work_dir_removed is False
        expected_order = [
            "child termination failed",
            "child process remained alive",
            "mock upstream shutdown failed",
            "mock upstream close failed",
            "work directory cleanup failed",
            "work directory remains after cleanup",
        ]
        assert len(cleanup_errors) == len(expected_order)
        for actual, expected_prefix in zip(cleanup_errors, expected_order, strict=True):
            assert actual.startswith(expected_prefix), (
                f"{actual!r} != {expected_prefix!r}"
            )
    if work_dir.exists():
        real_rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# run_validation setup failure and cancellation tests
# ---------------------------------------------------------------------------


def test_run_validation_setup_failure_and_cancellation_loop(tmp_path: Path) -> None:
    from scripts.run_dispatch_stability_soak import (
        ValidationRunConfig,
        run_validation,
    )

    plan = DurationPlan(
        total_s=12.0,
        warmup_s=1.0,
        early_window_s=4.0,
        drain_s=1.0,
        late_window_s=4.0,
        poll_interval_s=0.5,
        dispatch_p95_ratio_limit=10.0,
        dispatch_p99_ratio_limit=10.0,
        throughput_decline_limit=1.0,
        max_pending_requests=0,
        max_active_reservations=0,
    )

    for case, fail_target in [
        ("upstream_start", "scripts.run_dispatch_stability_soak._start_mock_upstream"),
        ("config_write", "scripts.run_dispatch_stability_soak._write_soak_config"),
        ("subprocess_start", "scripts.run_dispatch_stability_soak._start_eggpool"),
    ]:
        output = tmp_path / f"out-{case}.json"

        async def _run_setup(
            _ft: str = fail_target,
            _c: str = case,
            _o: Path = output,
        ) -> ValidationResult:
            with patch(_ft, side_effect=OSError(f"inject-{_c}")):
                return await run_validation(
                    ValidationRunConfig(
                        profile_name="balanced-file-backed",
                        duration_seconds=30,
                        output_path=_o,
                        seed=42,
                    ),
                    duration_plan=plan,
                    health_timeout_s=5.0,
                    quiescence_timeout_s=2.0,
                )

        result = asyncio.run(_run_setup())
        assert result.passed is False, case
        assert result.return_code != 0, case
        assert result.work_dir is not None, case
        assert not result.work_dir.exists(), (
            f"work directory not removed after {case}: {result.work_dir}"
        )
        assert result.process_stopped is None, case
        assert result.cleanup_errors == (), case
        assert result.process_log_tail == "", case
        assert any(
            "runtime validation internal error" in r for r in result.failure_reasons
        ), case
        assert output.is_file(), case
        loaded = json.loads(output.read_text())
        assert loaded["passed"] is False
        assert loaded["schema_version"] == SCHEMA_VERSION

    cancel_output = tmp_path / "out-cancel.json"

    async def _run_cancel() -> ValidationResult:
        async def _cancel_wait(*a: Any, **kw: Any) -> bool:
            raise asyncio.CancelledError()

        with patch(
            "scripts.run_dispatch_stability_soak._wait_healthy",
            side_effect=_cancel_wait,
        ):
            return await run_validation(
                ValidationRunConfig(
                    profile_name="balanced-file-backed",
                    duration_seconds=30,
                    output_path=cancel_output,
                    seed=42,
                ),
                duration_plan=plan,
                health_timeout_s=5.0,
                quiescence_timeout_s=2.0,
            )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run_cancel())


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
        self.cleanup_errors: tuple[str, ...] = ()


# Imported late so pytest always collects the module-level tests.
import tempfile  # noqa: E402
