"""Shared grouped-timeseries payload shaping."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

MIN_GROUPED_LIMIT = 1
MAX_GROUPED_LIMIT = 25

# Cap is comfortably above the largest expected chart width so legitimate
# padding always succeeds but pathological windows (multi-year spans with
# sparse data, or hand-crafted giant SQL windows) cannot produce tens of
# thousands of zero rows.
_MAX_ZERO_PAD_BUCKETS = 2048


def clamp_grouped_limit(limit: int) -> int:
    """Clamp grouped-timeseries top-N limits to the public API range."""
    return max(MIN_GROUPED_LIMIT, min(int(limit), MAX_GROUPED_LIMIT))


def empty_grouped_timeseries(bucket: str, group_by: str, limit: int) -> dict[str, Any]:
    """Return the stable zero-valued grouped timeseries contract."""
    limit = clamp_grouped_limit(limit)
    return {
        "bucket": bucket,
        "group_by": group_by,
        "metric": "requests",
        "limit": limit,
        "series": [],
        "buckets": [],
        "bucket_totals": [],
        "points": [],
    }


def postprocess_grouped_timeseries(
    raw_rows: Sequence[Mapping[str, Any]],
    *,
    bucket: str,
    group_by: str,
    limit: int,
) -> dict[str, Any]:
    """Fold raw grouped rows into the dashboard/API timeseries payload."""
    limit = clamp_grouped_limit(limit)
    if not raw_rows:
        return empty_grouped_timeseries(bucket, group_by, limit)

    series_totals: dict[str, int] = {}
    for row in raw_rows:
        key = str(row["raw_series_key"])
        series_totals[key] = series_totals.get(key, 0) + int(row["request_count"])

    ranked_keys = sorted(
        series_totals.keys(),
        key=lambda k: (-series_totals[k], k),
    )
    top_keys = set(ranked_keys[:limit])
    include_other = len(top_keys) < len(series_totals)

    bucket_set: set[str] = set()
    fold: dict[tuple[str, str], dict[str, Any]] = {}
    for row in raw_rows:
        bucket_label = str(row["bucket"])
        bucket_set.add(bucket_label)
        raw_key = str(row["raw_series_key"])
        is_other_row = raw_key not in top_keys
        if is_other_row:
            series_key = "__other__"
            label = "Other"
            provider_id: str | None = None
            model_id: str | None = None
            account_name: str | None = None
        else:
            series_key = raw_key
            label = str(row["raw_series_label"])
            provider_id = _optional_str(row.get("provider_id"))
            model_id = _optional_str(row.get("model_id"))
            account_name = _optional_str(row.get("account_name"))

        fold_key = (bucket_label, series_key)
        entry = fold.get(fold_key)
        if entry is None:
            entry = _new_point(
                bucket=bucket_label,
                series_key=series_key,
                label=label,
                provider_id=provider_id,
                model_id=model_id,
                account_name=account_name,
                is_other=is_other_row,
            )
            fold[fold_key] = entry
        _accumulate_point(entry, row)

    points = [_finish_point(entry) for entry in fold.values()]
    series_out = _build_series(points, ranked_keys, top_keys, include_other)
    bucket_totals_out = _build_bucket_totals(points, bucket_set)

    points.sort(
        key=lambda p: (
            p["bucket"],
            1 if p["is_other"] else 0,
            p["label"],
        )
    )

    buckets = _pad_grouped_buckets(sorted(bucket_set), bucket)
    bucket_totals_out = _reindex_bucket_totals(bucket_totals_out, buckets)
    points = _extend_grouped_points(points, series_out, buckets)

    return {
        "bucket": bucket,
        "group_by": group_by,
        "metric": "requests",
        "limit": limit,
        "series": series_out,
        "buckets": buckets,
        "bucket_totals": bucket_totals_out,
        "points": points,
    }


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _new_point(
    *,
    bucket: str,
    series_key: str,
    label: str,
    provider_id: str | None,
    model_id: str | None,
    account_name: str | None,
    is_other: bool,
) -> dict[str, Any]:
    return {
        "bucket": bucket,
        "series_key": series_key,
        "label": label,
        "provider_id": provider_id,
        "model_id": model_id,
        "account_name": account_name,
        "is_other": is_other,
        "request_count": 0,
        "error_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "cost_microdollars": 0,
        "bytes_received": 0,
        "bytes_emitted": 0,
        "_weighted_latency_num": 0.0,
        "_weighted_ttft_num": 0.0,
    }


def _accumulate_point(entry: dict[str, Any], row: Mapping[str, Any]) -> None:
    request_count = int(row["request_count"])
    for field in (
        "request_count",
        "error_count",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "total_tokens",
        "cost_microdollars",
        "bytes_received",
        "bytes_emitted",
    ):
        entry[field] = int(entry[field]) + int(row[field])

    if request_count > 0:
        entry["_weighted_latency_num"] = float(entry["_weighted_latency_num"]) + (
            float(row["avg_latency_ms"]) * request_count
        )
        entry["_weighted_ttft_num"] = float(entry["_weighted_ttft_num"]) + (
            float(row["avg_ttft_ms"]) * request_count
        )


def _finish_point(entry: dict[str, Any]) -> dict[str, Any]:
    request_count = int(entry["request_count"])
    if request_count > 0:
        entry["avg_latency_ms"] = float(entry["_weighted_latency_num"]) / request_count
        entry["avg_ttft_ms"] = float(entry["_weighted_ttft_num"]) / request_count
    else:
        entry["avg_latency_ms"] = 0.0
        entry["avg_ttft_ms"] = 0.0
    del entry["_weighted_latency_num"]
    del entry["_weighted_ttft_num"]
    return entry


def _new_series_summary(key: str, *, is_other: bool = False) -> dict[str, Any]:
    return {
        "key": key,
        "label": "Other" if is_other else "",
        "provider_id": None,
        "model_id": None,
        "account_name": None,
        "is_other": is_other,
        "total_requests": 0,
        "error_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "cost_microdollars": 0,
        "bytes_received": 0,
        "bytes_emitted": 0,
        "_weighted_latency_num": 0.0,
        "_weighted_ttft_num": 0.0,
    }


def _build_series(
    points: Sequence[Mapping[str, Any]],
    ranked_keys: Sequence[str],
    top_keys: set[str],
    include_other: bool,
) -> list[dict[str, Any]]:
    summaries = {key: _new_series_summary(key) for key in top_keys}
    if include_other:
        summaries["__other__"] = _new_series_summary("__other__", is_other=True)

    for point in points:
        summary = summaries.get(str(point["series_key"]))
        if summary is None:
            continue
        _accumulate_series_summary(summary, point)

    series_out = [
        _finish_series_summary(summaries[key]) for key in ranked_keys if key in top_keys
    ]
    if include_other:
        series_out.append(_finish_series_summary(summaries["__other__"]))
    return series_out


def _accumulate_series_summary(
    summary: dict[str, Any], point: Mapping[str, Any]
) -> None:
    request_count = int(point["request_count"])
    summary["total_requests"] += request_count
    for field in (
        "error_count",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "total_tokens",
        "cost_microdollars",
        "bytes_received",
        "bytes_emitted",
    ):
        summary[field] += int(point[field])

    if request_count > 0:
        summary["_weighted_latency_num"] += (
            float(point["avg_latency_ms"]) * request_count
        )
        summary["_weighted_ttft_num"] += float(point["avg_ttft_ms"]) * request_count

    if summary["is_other"]:
        return
    if not summary["label"] and point["label"]:
        summary["label"] = point["label"]
    if summary["provider_id"] is None and point["provider_id"] is not None:
        summary["provider_id"] = point["provider_id"]
    if summary["model_id"] is None and point["model_id"] is not None:
        summary["model_id"] = point["model_id"]
    if summary["account_name"] is None and point["account_name"] is not None:
        summary["account_name"] = point["account_name"]


def _finish_series_summary(summary: dict[str, Any]) -> dict[str, Any]:
    total_requests = int(summary["total_requests"])
    if total_requests > 0:
        summary["avg_latency_ms"] = (
            float(summary["_weighted_latency_num"]) / total_requests
        )
        summary["avg_ttft_ms"] = float(summary["_weighted_ttft_num"]) / total_requests
    else:
        summary["avg_latency_ms"] = 0.0
        summary["avg_ttft_ms"] = 0.0
    del summary["_weighted_latency_num"]
    del summary["_weighted_ttft_num"]
    return summary


def _build_bucket_totals(
    points: Sequence[Mapping[str, Any]], bucket_set: set[str]
) -> list[dict[str, Any]]:
    buckets = {
        bucket_label: _new_bucket_total(bucket_label) for bucket_label in bucket_set
    }
    for point in points:
        bucket = buckets[str(point["bucket"])]
        request_count = int(point["request_count"])
        bucket["request_count"] += request_count
        for field in (
            "error_count",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "total_tokens",
            "cost_microdollars",
            "bytes_received",
            "bytes_emitted",
        ):
            bucket[field] += int(point[field])
        bucket["_weighted_latency_num"] += (
            float(point["avg_latency_ms"]) * request_count
        )
        bucket["_weighted_ttft_num"] += float(point["avg_ttft_ms"]) * request_count

    return [
        _finish_bucket_total(buckets[bucket_label]) for bucket_label in sorted(buckets)
    ]


def _new_bucket_total(bucket: str) -> dict[str, Any]:
    return {
        "bucket": bucket,
        "request_count": 0,
        "error_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "cost_microdollars": 0,
        "bytes_received": 0,
        "bytes_emitted": 0,
        "_weighted_latency_num": 0.0,
        "_weighted_ttft_num": 0.0,
    }


def _finish_bucket_total(bucket: dict[str, Any]) -> dict[str, Any]:
    request_count = int(bucket["request_count"])
    if request_count > 0:
        bucket["avg_latency_ms"] = (
            float(bucket["_weighted_latency_num"]) / request_count
        )
        bucket["avg_ttft_ms"] = float(bucket["_weighted_ttft_num"]) / request_count
    else:
        bucket["avg_latency_ms"] = 0.0
        bucket["avg_ttft_ms"] = 0.0
    del bucket["_weighted_latency_num"]
    del bucket["_weighted_ttft_num"]
    return bucket


def _pad_grouped_buckets(buckets: list[str], bucket: str) -> list[str]:
    """Fill missing bucket labels between the first and last seen.

    Returns ``buckets`` unchanged when fewer than two labels are present
    (no useful spine to extrapolate) or when the bucket size is
    unrecognized.  Otherwise returns every bucket label from the first to
    the last inclusive at the chosen granularity.
    """
    if len(buckets) < 2 or bucket not in ("hour", "day"):
        return buckets
    span = _bucket_label_span(buckets[0], buckets[-1], bucket)
    if span is None:
        return buckets
    return span


def _reindex_bucket_totals(
    totals: list[dict[str, Any]], buckets: list[str]
) -> list[dict[str, Any]]:
    """Re-emit bucket_totals so every padded bucket has a row.

    Preserves the existing rows (and their aggregations) verbatim; for
    any padded bucket between the first and last totals, inserts a
    zero-valued entry that matches the bucket_totals column shape.
    """
    if not totals or len(buckets) < 2:
        return totals
    by_label = {str(row["bucket"]): row for row in totals}
    padded: list[dict[str, Any]] = []
    zero_inserted = 0
    for label in buckets:
        existing = by_label.get(label)
        if existing is not None:
            padded.append(existing)
        else:
            padded.append(_new_bucket_total(label))
            zero_inserted += 1
            if zero_inserted > _MAX_ZERO_PAD_BUCKETS:
                # Append any remaining original totals that fall inside
                # the still-pending portion of the spine so callers see
                # a strictly monotonic prefix instead of an arbitrary
                # truncation.
                for follow_label in buckets[len(padded) :]:
                    follow_row = by_label.get(follow_label)
                    if follow_row is not None:
                        padded.append(follow_row)
                return padded
    return padded


def _extend_grouped_points(
    points: list[dict[str, Any]],
    series: list[dict[str, Any]],
    buckets: list[str],
) -> list[dict[str, Any]]:
    """Insert zero-valued points for every series in every padded bucket.

    The grouped chart renders one polyline per series across the full
    x-axis.  Without padding each series disappears for hours it had no
    traffic, producing broken line segments.  This helper emits a
    zero-valued point for every (series_key, missing_bucket) pair so
    each line stays anchored to the axis.
    """
    if len(buckets) < 2 or not series:
        return points
    series_keys_seen = {str(point["series_key"]) for point in points}
    series_keys = [str(s["key"]) for s in series]
    buckets_with_data = {str(p["bucket"]) for p in points}
    padded_point_count = 0
    new_points: list[dict[str, Any]] = []
    for bucket_label in buckets:
        for series_key, series_summary in zip(series_keys, series, strict=False):
            if bucket_label in buckets_with_data:
                continue
            if series_key not in series_keys_seen and not _series_covers_buckets(
                series_summary
            ):
                continue
            is_other = bool(series_summary.get("is_other"))
            new_points.append(
                _zero_point(
                    bucket_label,
                    series_key,
                    is_other=is_other,
                    series_summary=series_summary,
                )
            )
            padded_point_count += 1
            if padded_point_count > _MAX_ZERO_PAD_BUCKETS * max(1, len(series_keys)):
                return points + new_points
    return points + new_points


def _series_covers_buckets(summary: dict[str, Any]) -> bool:
    """Return whether a series should be padded across empty buckets.

    Top-N series and the synthetic ``__other__`` bucket both deserve
    full padding so the chart's lines stay continuous.  External
    callers should not see this helper.
    """
    return True


def _zero_point(
    bucket: str,
    series_key: str,
    *,
    is_other: bool,
    series_summary: dict[str, Any],
) -> dict[str, Any]:
    """Return a zero-valued point matching ``_new_point``'s shape."""
    if is_other:
        return _new_point(
            bucket=bucket,
            series_key=series_key,
            label="Other",
            provider_id=None,
            model_id=None,
            account_name=None,
            is_other=True,
        )
    return _new_point(
        bucket=bucket,
        series_key=series_key,
        label=str(series_summary.get("label") or series_key),
        provider_id=series_summary.get("provider_id"),
        model_id=series_summary.get("model_id"),
        account_name=series_summary.get("account_name"),
        is_other=False,
    )


def _bucket_label_span(first: str, last: str, bucket: str) -> list[str] | None:
    """Return every bucket label between two endpoints at the chosen size.

    Both labels are inclusive.  Returns ``None`` when the endpoints
    cannot be parsed or are out of order so the caller can fall back to
    the raw rows.
    """
    fmt = "%Y-%m-%d %H:00:00" if bucket == "hour" else "%Y-%m-%d 00:00:00"
    try:
        first_dt = datetime.strptime(first, fmt)
        last_dt = datetime.strptime(last, fmt)
    except ValueError:
        return None
    if last_dt < first_dt:
        return None
    step = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
    labels: list[str] = []
    cursor = first_dt
    while cursor <= last_dt:
        labels.append(cursor.strftime(fmt))
        cursor = cursor + step
    return labels
