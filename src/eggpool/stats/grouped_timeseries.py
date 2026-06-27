"""Shared grouped-timeseries payload shaping."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


def empty_grouped_timeseries(bucket: str, group_by: str, limit: int) -> dict[str, Any]:
    """Return the stable zero-valued grouped timeseries contract."""
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
    include_other = len(top_keys) < len(ranked_keys)

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

    return {
        "bucket": bucket,
        "group_by": group_by,
        "metric": "requests",
        "limit": limit,
        "series": series_out,
        "buckets": sorted(bucket_set),
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
    bucket_totals: list[dict[str, Any]] = []
    for bucket_label in sorted(bucket_set):
        bucket_points = [p for p in points if p["bucket"] == bucket_label]
        request_count = sum(int(p["request_count"]) for p in bucket_points)
        weighted_latency_num = sum(
            float(p["avg_latency_ms"]) * int(p["request_count"]) for p in bucket_points
        )
        weighted_ttft_num = sum(
            float(p["avg_ttft_ms"]) * int(p["request_count"]) for p in bucket_points
        )
        bucket_totals.append(
            {
                "bucket": bucket_label,
                "request_count": request_count,
                "error_count": _sum_points(bucket_points, "error_count"),
                "input_tokens": _sum_points(bucket_points, "input_tokens"),
                "output_tokens": _sum_points(bucket_points, "output_tokens"),
                "cache_read_tokens": _sum_points(bucket_points, "cache_read_tokens"),
                "cache_write_tokens": _sum_points(bucket_points, "cache_write_tokens"),
                "reasoning_tokens": _sum_points(bucket_points, "reasoning_tokens"),
                "total_tokens": _sum_points(bucket_points, "total_tokens"),
                "cost_microdollars": _sum_points(bucket_points, "cost_microdollars"),
                "bytes_received": _sum_points(bucket_points, "bytes_received"),
                "bytes_emitted": _sum_points(bucket_points, "bytes_emitted"),
                "avg_latency_ms": (
                    weighted_latency_num / request_count if request_count > 0 else 0.0
                ),
                "avg_ttft_ms": (
                    weighted_ttft_num / request_count if request_count > 0 else 0.0
                ),
            }
        )
    return bucket_totals


def _sum_points(points: Sequence[Mapping[str, Any]], field: str) -> int:
    return sum(int(point[field]) for point in points)
