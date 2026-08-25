"""Tests for the structured logging formatters."""

from __future__ import annotations

import json
import logging

import pytest

from eggpool.logging import _HumanFormatter, _JsonFormatter


@pytest.fixture(autouse=True)
def _restore_root_handlers():
    """Keep root logger configuration isolated per test."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def _record_with_extra(extra: dict[str, object]) -> logging.LogRecord:
    """Build a record whose ``extra`` attribute matches formatter expectations."""
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    record.__dict__["extra"] = extra
    return record


class TestJsonFormatter:
    def test_serializes_json_extras(self) -> None:
        formatted = _JsonFormatter().format(_record_with_extra({"request_id": "r-1"}))
        payload = json.loads(formatted)
        assert payload["message"] == "hello"
        assert payload["extra"] == {"request_id": "r-1"}

    def test_non_serializable_extra_does_not_raise(self) -> None:
        """Non-JSON-serializable extras fall back to ``str`` (B9)."""

        class Opaque:
            def __str__(self) -> str:
                return "<opaque>"

        formatted = _JsonFormatter().format(
            _record_with_extra({"obj": Opaque(), "type": object})
        )
        payload = json.loads(formatted)
        assert payload["extra"]["obj"] == "<opaque>"
        assert payload["extra"]["type"] == str(object)


class TestHumanFormatter:
    def test_plain_format(self) -> None:
        formatted = _HumanFormatter().format(_record_with_extra({}))
        assert "hello" in formatted
        assert "test.logger" in formatted
