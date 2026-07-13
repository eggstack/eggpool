"""Period-aware bucket selection for the timeseries dashboard.

The dashboard used to force operators to pick an explicit bucket
(``hour`` or ``day``) for every period.  That is fragile: ``30d`` with
``hour`` buckets produces 720 bins and overwhelms the chart, while
``24h`` with ``hour`` buckets shows sparse data when the window only
covers the recent uptime.  This module makes the bucket size a function
of the selected period and exposes a single ``default_bucket_for_period``
helper that the route handlers consult when the client does not pin a
bucket explicitly.
"""

from __future__ import annotations

from eggpool.stats import PERIOD_PRESETS

_AUTO_BUCKET_RULES: tuple[tuple[int, str], ...] = (
    (7 * 86400, "hour"),
    (30 * 86400, "day"),
)

AUTO_BUCKET = "auto"
VALID_BUCKETS_WITH_AUTO: frozenset[str] = frozenset({"hour", "day", AUTO_BUCKET})


def default_bucket_for_period(period: str | None) -> str:
    """Return the recommended bucket size for a dashboard period preset.

    ``1h`` and ``24h`` continue to use hourly buckets.  ``7d`` keeps
    hourly because 168 bars still fit comfortably on the chart.  ``30d``
    flips to daily so the chart stays readable.  Unknown periods (and
    ``None``) fall back to ``"hour"`` to preserve today's behaviour.

    The bucket size is a chart-presentation concern — the underlying
    ``usage_rollups`` table continues to be written at its configured
    60-second granularity and the SQL still groups by ``strftime`` over
    that raw data.
    """
    if not period:
        return "hour"
    seconds = PERIOD_PRESETS.get(period)
    if seconds is None:
        return "hour"
    for max_seconds, bucket in _AUTO_BUCKET_RULES:
        if seconds <= max_seconds:
            return bucket
    return "day"


def resolve_bucket(
    bucket: str | None,
    period: str | None,
) -> str:
    """Return a normalized bucket, expanding ``"auto"`` via the period.

    Accepts ``None`` and the empty string as "client did not pick a
    bucket" — in that case the result is chosen via
    :func:`default_bucket_for_period`.  Any unrecognized non-auto value
    falls back to ``"hour"`` to preserve today's normalization.
    """
    if bucket in (None, "", AUTO_BUCKET):
        return default_bucket_for_period(period)
    return bucket if bucket in {"hour", "day"} else "hour"
