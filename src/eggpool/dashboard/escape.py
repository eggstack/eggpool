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


def format_duration_ms(value: float | int | None) -> str:
    """Format a millisecond duration as a human-readable short string.

    Buckets: ``<1s`` shows ms, ``<60s`` shows seconds with one decimal,
    ``<1h`` shows ``MmSs``, ``<1d`` shows ``HhMm``, otherwise days.
    Returns ``"—"`` for ``None`` or negative values.
    """
    if value is None:
        return "—"
    ms = float(value)
    if ms < 0:
        return "—"
    if ms < 1000:
        return f"{ms:.0f} ms"
    seconds = ms / 1000.0
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{sec}s"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{mins}m"
    days, hrs = divmod(hours, 24)
    return f"{days}d{hrs}h"


def format_age_seconds(value: float | int | None) -> str:
    """Format an age in seconds as a human-readable string.

    Returns ``"—"`` for ``None`` or negative values.  Suitable for
    "oldest pending age" cards where operators want a quick sense of
    scale.
    """
    if value is None:
        return "—"
    seconds = float(value)
    if seconds < 0:
        return "—"
    if seconds < 1:
        return "<1s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{sec}s"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{mins}m"
    days, hrs = divmod(hours, 24)
    return f"{days}d{hrs}h"


def format_percent100(value: float | int | None, digits: int = 1) -> str:
    """Format a percentage value that is already in 0–100 scale.

    Many stats endpoints expose cost-fractions and ratios as percentages
    directly (0..100) rather than fractions (0..1).  This helper handles
    the percent convention; use :func:`format_percent` for fractions.
    """
    if value is None:
        value = 0.0
    return f"{float(value):.{digits}f}%"


def format_percent01(value: float | int | None, digits: int = 2) -> str:
    """Alias for :func:`format_percent` for explicit ratio convention.

    Provided so call sites can document which convention they expect
    from upstream (ratio vs percent) without ambiguity.
    """
    return format_percent(value, digits)  # type: ignore[arg-type]


def format_int(value: int | None) -> str:
    """Format an integer with thousands separators.

    Returns ``"—"`` for ``None`` so empty groups don't render as ``0``
    and confuse the operator.  Zero still renders as ``"0"`` because it
    is a real value.
    """
    if value is None:
        return "—"
    return f"{int(value):,}"


def format_count_or_dash(value: int | float | None) -> str:
    """Format a numeric count, rendering ``—`` for ``None``.

    Distinct from :func:`format_int` in that it accepts floats so it
    can be used for aggregated counts that may come back as floats
    (e.g., counts averaged across providers).
    """
    if value is None:
        return "—"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:.2f}"
    return f"{int(value):,}"


def short_id(value: str | None, length: int = 8) -> str:
    """Return a short prefix of an identifier for compact table display.

    Long hex/UUID prefixes are common in proxy_request_id and
    upstream_request_id.  Showing only the leading ``length`` chars
    keeps tables compact while still letting operators correlate
    against log lines.

    Returns ``"—"`` for ``None`` or empty input.
    """
    if not value:
        return "—"
    text = str(value)
    if len(text) <= length:
        return text
    return text[:length]


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
