"""Tests for structured logging redaction."""

from __future__ import annotations

import json
import logging

from eggpool.logging import _JsonFormatter


def test_json_formatter_redacts_sensitive_extra_values() -> None:
    record = logging.LogRecord(
        name="eggpool.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="request completed",
        args=(),
        exc_info=None,
    )
    record.profile = "production"
    record.api_key = "sk-secret-value-123456"
    record.nested = {"authorization": "Bearer secret-token"}

    payload = json.loads(_JsonFormatter().format(record))

    assert payload["extra"]["profile"] == "production"
    assert payload["extra"]["api_key"] == "[REDACTED]"
    assert payload["extra"]["nested"]["authorization"] == "[REDACTED]"
