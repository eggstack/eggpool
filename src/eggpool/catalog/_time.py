"""Shared catalog timestamp conversion helpers."""

from __future__ import annotations

import datetime as _dt


def ts_to_unix(value: object) -> float:
    """Convert a DB timestamp string (or numeric) to a Unix float.

    Returns 0.0 for None or unparseable values so cache loads never fail on a
    malformed timestamp. Naive datetime strings are treated as UTC, matching
    SQLite's CURRENT_TIMESTAMP convention.
    """
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        dt = _dt.datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.UTC)
        return dt.timestamp()
    except ValueError:
        try:
            dt = _dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            dt = dt.replace(tzinfo=_dt.UTC)
            return dt.timestamp()
        except ValueError:
            return 0.0
