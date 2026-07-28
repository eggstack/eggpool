"""JSON operation counters (unit).

Validates the ``JSONOperationCounters`` counting layer: install/uninstall,
context switching, snapshot/reset, and thread safety.
"""

from __future__ import annotations

import eggpool.jsonx as jsonx
from tests.support.json_counters import CounterSnapshot, JSONOperationCounters


class TestJSONOperationCounters:
    def test_install_and_uninstall(self) -> None:
        counters = JSONOperationCounters()
        assert not counters.is_installed()
        counters.install()
        assert counters.is_installed()
        counters.uninstall()
        assert not counters.is_installed()

    def test_double_install_is_idempotent(self) -> None:
        counters = JSONOperationCounters()
        counters.install()
        counters.install()
        assert counters.is_installed()
        counters.uninstall()

    def test_double_uninstall_is_idempotent(self) -> None:
        counters = JSONOperationCounters()
        counters.uninstall()
        assert not counters.is_installed()

    def test_counts_request_decode(self) -> None:
        counters = JSONOperationCounters()
        counters.install()
        try:
            counters.set_context("request_decode")
            jsonx.loads(b'{"key": "value"}')
            jsonx.loads(b'{"key": "value"}')
            snap = counters.snapshot()
            assert snap.request_decode == 2
            assert snap.total == 2
        finally:
            counters.uninstall()

    def test_counts_request_encode(self) -> None:
        counters = JSONOperationCounters()
        counters.install()
        try:
            counters.set_context("request_encode")
            jsonx.dumps_bytes({"key": "value"})
            snap = counters.snapshot()
            assert snap.request_encode == 1
            assert snap.total == 1
        finally:
            counters.uninstall()

    def test_counts_response_decode(self) -> None:
        counters = JSONOperationCounters()
        counters.install()
        try:
            counters.set_context("response_decode")
            jsonx.loads(b'{"status": "ok"}')
            snap = counters.snapshot()
            assert snap.response_decode == 1
        finally:
            counters.uninstall()

    def test_counts_stream_event_decode(self) -> None:
        counters = JSONOperationCounters()
        counters.install()
        try:
            counters.set_context("stream_event_decode")
            jsonx.loads(b'{"choices": []}')
            snap = counters.snapshot()
            assert snap.stream_event_decode == 1
        finally:
            counters.uninstall()

    def test_context_switching(self) -> None:
        counters = JSONOperationCounters()
        counters.install()
        try:
            counters.set_context("request_decode")
            jsonx.loads(b'{"a": 1}')
            counters.set_context("response_encode")
            jsonx.dumps_bytes({"b": 2})
            snap = counters.snapshot()
            assert snap.request_decode == 1
            assert snap.response_encode == 1
            assert snap.total == 2
        finally:
            counters.uninstall()

    def test_reset(self) -> None:
        counters = JSONOperationCounters()
        counters.install()
        try:
            counters.set_context("request_decode")
            jsonx.loads(b'{"a": 1}')
            counters.reset()
            snap = counters.snapshot()
            assert snap.total == 0
        finally:
            counters.uninstall()

    def test_snapshot_returns_immutable_copy(self) -> None:
        counters = JSONOperationCounters()
        counters.install()
        try:
            counters.set_context("request_decode")
            jsonx.loads(b'{"a": 1}')
            snap1 = counters.snapshot()
            snap2 = counters.snapshot()
            assert snap1 == snap2
            counters.reset()
            assert snap1.request_decode == 1
        finally:
            counters.uninstall()

    def test_to_dict(self) -> None:
        snap = CounterSnapshot(request_decode=3, response_encode=2)
        d = snap.to_dict()
        assert d["request_decode"] == 3
        assert d["response_encode"] == 2
        assert d["total"] == 5

    def test_total_properties(self) -> None:
        snap = CounterSnapshot(
            request_decode=1,
            request_encode=2,
            response_decode=3,
            response_encode=4,
            stream_event_decode=5,
            stream_event_encode=6,
        )
        assert snap.total_decode == 9
        assert snap.total_encode == 12
        assert snap.total == 21

    def test_counter_functionality_after_uninstall(self) -> None:
        counters = JSONOperationCounters()
        counters.install()
        counters.uninstall()
        data = jsonx.loads(b'{"test": true}')
        assert data["test"] is True
        result = jsonx.dumps_bytes({"test": True})
        assert b"test" in result
