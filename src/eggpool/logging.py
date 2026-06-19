"""Structured logging setup for the aggregator."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any


class _HumanFormatter(logging.Formatter):
    """Plain human-readable log format."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )


class _JsonFormatter(logging.Formatter):
    """Structured JSON log formatter."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.__dict__.get("extra"):
            payload["extra"] = record.extra  # type: ignore[attr-defined]
        return json.dumps(payload)


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    """Configure root logger with the chosen formatter.

    Args:
        level: Log level name (e.g. ``"DEBUG"``, ``"INFO"``).
        json_output: When *True*, emit JSON lines instead of plain text.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter() if json_output else _HumanFormatter())
    root.handlers.clear()
    root.addHandler(handler)
