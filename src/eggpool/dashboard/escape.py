"""HTML escape utilities for the dashboard.

All free-text fields rendered into HTML templates must be escaped
to prevent HTML injection (model_id, account_name, error_message, etc.).
"""

from __future__ import annotations

import html
import re
from typing import Any

_PATTERN: re.Pattern[str] = re.compile(r"[^a-zA-Z0-9_-]")


def escape(value: Any) -> str:
    """Escape a value for safe inclusion in HTML.

    None becomes the empty string. Other values are coerced to str
    then HTML-escaped.
    """
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def escape_attr(value: Any) -> str:
    """Escape a value for use in an HTML attribute (quotes always escaped)."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def format_microdollars(value: int | float | None) -> str:
    """Format a microdollar value as $X.XXXXXX."""
    if value is None:
        value = 0
    return f"${value / 1_000_000:.6f}"


def format_tokens(value: int | None) -> str:
    """Format a token count with thousands separators."""
    if value is None:
        value = 0
    return f"{int(value):,}"


def format_tokens_per_second(value: float | None) -> str:
    """Format a throughput value as a 'tok/s' string.

    Returns ``"—"`` for ``None`` and for non-positive values so empty
    groups don't render as noisy zeros.  Positive values render with one
    decimal place which keeps both small (<10) and large (>1000)
    throughputs readable at a glance.
    """
    if value is None:
        return "—"
    if float(value) <= 0:
        return "—"
    return f"{float(value):.1f} tok/s"


def format_percent(value: float | None, digits: int = 2) -> str:
    """Format a fraction as a percentage."""
    if value is None:
        value = 0.0
    return f"{value * 100:.{digits}f}%"


def format_latency(value: float | None) -> str:
    """Format a latency value in milliseconds."""
    if value is None:
        value = 0.0
    return f"{float(value):.1f} ms"


def format_bytes(value: int | float | None) -> str:
    """Format a byte count as a human-readable string (B, KB, MB, GB, TB).

    Uses 1000-based (SI) divisions for network bandwidth readability.
    """
    if value is None:
        value = 0
    value = float(value)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if abs(value) < 1000.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1000.0
    return f"{value:.1f} PB"


def format_timestamp(value: Any) -> str:
    """Format a timestamp for display."""
    if value is None:
        return ""
    return str(value)


def truncate(value: Any, max_length: int = 80) -> str:
    """Truncate a string to a maximum length, escaping the result."""
    escaped = escape(value)
    if len(escaped) <= max_length:
        return escaped
    truncated = escaped[: max_length - 3]
    # Don't break an HTML entity mid-entity — back up to the last '&'
    amp_pos = truncated.rfind("&")
    if amp_pos != -1 and ";" not in truncated[amp_pos:]:
        truncated = truncated[:amp_pos]
    return truncated + "..."


def sanitize_class_name(value: str) -> str:
    """Sanitize a string for use as an HTML class name."""
    if not value:
        return ""
    return _PATTERN.sub("_", str(value))
