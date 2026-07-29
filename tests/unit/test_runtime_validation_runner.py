from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.run_dispatch_stability_soak import (
    _build_parser,
    _memory_total_bytes,
    _write_atomic_json,
    build_duration_plan,
    evaluate_drain_gate,
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
