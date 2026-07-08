"""Canonical provider-neutral cache metric derivation and aggregation.

This module implements the per-protocol cache hit rate logic specified in
plans/2026-07-08-provider-cache-hit-metric-and-index-card-tightening.md.

Key invariants:

- Anthropic ``input_tokens`` is fresh-only; the eligible denominator is
  ``input + cache_read + cache_creation``.
- OpenAI ``prompt_tokens`` is total-billed (includes cached); the
  eligible denominator is ``prompt_tokens``.
- Cache reads are hits; cache writes/creation are warmup, not hits.
- Missing or malformed cache counters are never conflated with zero hits.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

from eggpool.proxy.normalized_usage import CacheCounterStatus


class ProtocolShape(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CacheMetricTerms:
    cache_read_tokens: int
    cache_write_tokens: int
    cache_eligible_input_tokens: int
    cache_read_clamped: bool
    cache_write_clamped: bool
    protocol_shape: ProtocolShape


@dataclass(frozen=True)
class AggregatedCacheMetrics:
    cache_read_tokens_canonical: int
    cache_write_tokens_canonical: int
    cache_eligible_input_tokens: int
    cache_benefited_requests: int
    cache_eligible_requests: int
    cache_counter_reported_requests: int
    cache_counter_not_reported_requests: int
    cache_counter_unknown_requests: int
    inconsistent_cache_counter_rows: int
    provider_cache_hit_rate: float | None
    cache_write_rate: float | None
    cache_benefited_request_rate: float | None
    cache_counter_coverage_rate: float | None


def _map_protocol(protocol: str | None) -> ProtocolShape:
    if protocol == "anthropic":
        return ProtocolShape.ANTHROPIC
    if protocol == "openai":
        return ProtocolShape.OPENAI
    return ProtocolShape.UNKNOWN


def derive_cache_metric_terms(
    *,
    input_tokens: int | None,
    cache_read_tokens: int | None,
    cache_write_tokens: int | None,
    protocol: str | None,
) -> CacheMetricTerms:
    shape = _map_protocol(protocol)
    inp = max(0, input_tokens or 0)
    read = max(0, cache_read_tokens or 0)
    write = max(0, cache_write_tokens or 0)
    has_input = input_tokens is not None and input_tokens > 0
    read_clamped = has_input and read > inp
    write_clamped = has_input and write > inp
    if read_clamped:
        read = inp
    if write_clamped:
        write = inp
    if shape == ProtocolShape.OPENAI:
        eligible = inp
    else:
        eligible = inp + read + write if has_input else read + write
    return CacheMetricTerms(
        cache_read_tokens=read,
        cache_write_tokens=write,
        cache_eligible_input_tokens=eligible,
        cache_read_clamped=read_clamped,
        cache_write_clamped=write_clamped,
        protocol_shape=shape,
    )


def aggregate_cache_terms(
    rows: Iterable[CacheMetricTerms],
    statuses: Iterable[CacheCounterStatus],
) -> AggregatedCacheMetrics:
    total_read = 0
    total_write = 0
    total_eligible = 0
    benefited = 0
    eligible_count = 0
    reported_count = 0
    not_reported_count = 0
    unknown_count = 0
    inconsistent = 0
    for terms, status in zip(rows, statuses, strict=True):
        if status == CacheCounterStatus.REPORTED:
            reported_count += 1
        elif status == CacheCounterStatus.NOT_REPORTED:
            not_reported_count += 1
        else:
            unknown_count += 1
        if status != CacheCounterStatus.REPORTED:
            continue
        total_read += terms.cache_read_tokens
        total_write += terms.cache_write_tokens
        total_eligible += terms.cache_eligible_input_tokens
        if terms.cache_read_tokens > 0:
            benefited += 1
        if terms.cache_eligible_input_tokens > 0:
            eligible_count += 1
        if terms.cache_read_tokens > terms.cache_eligible_input_tokens:
            inconsistent += 1
    hit_rate = total_read / total_eligible if total_eligible > 0 else None
    write_rate = total_write / total_eligible if total_eligible > 0 else None
    benefit_rate = benefited / eligible_count if eligible_count > 0 else None
    total_all = reported_count + not_reported_count + unknown_count
    coverage_rate = reported_count / total_all if total_all > 0 else None
    return AggregatedCacheMetrics(
        cache_read_tokens_canonical=total_read,
        cache_write_tokens_canonical=total_write,
        cache_eligible_input_tokens=total_eligible,
        cache_benefited_requests=benefited,
        cache_eligible_requests=eligible_count,
        cache_counter_reported_requests=reported_count,
        cache_counter_not_reported_requests=not_reported_count,
        cache_counter_unknown_requests=unknown_count,
        inconsistent_cache_counter_rows=inconsistent,
        provider_cache_hit_rate=hit_rate,
        cache_write_rate=write_rate,
        cache_benefited_request_rate=benefit_rate,
        cache_counter_coverage_rate=coverage_rate,
    )
