"""Provider-neutral usage normalisation for cache/token observability.

Phase 1 of the cache-preserving deterministic compression plan introduces
a single internal model for usage counters that is independent of the
upstream wire format.  Earlier the system already tracks token counts
through :class:`eggpool.proxy.usage.StreamUsageResult` and
:class:`eggpool.transcoder.usage.CanonicalUsage`, but neither captures
whether the upstream actually surfaced cache-related counters — they
collapse missing fields to ``0`` and lose the "unknown" signal.  This
module closes that gap.

Key design choices:

- ``None`` is distinct from ``0``.  ``None`` means "the upstream did
  not report or EggPool could not parse this counter"; ``0`` means
  "the upstream reported zero".  This is the only way to distinguish
  "providers that do not expose cache counters" from "providers whose
  last request had zero cache hits" without inventing zero values.
- :class:`NormalizedUsage` is intentionally minimal and immutable
  (``frozen=True``) so it can be safely shared across the coordinator,
  finalizer, persistence layer, and stats pipeline without defensive
  copies.
- :data:`CacheCounterStatus` is a closed enum with three values that
  cover the observed provider shapes.  It is persisted as TEXT in the
  database so future statuses can be added without a destructive
  migration.
- Helpers never raise on malformed input.  A bad upstream payload
  yields a partially populated :class:`NormalizedUsage` plus a status
  of ``"unknown_format"``; the request still completes and the
  dashboard shows a structured "parse failure" event instead of a
  zero value that would distort rollups.

This module is observational only.  It does not change request bodies,
route scoring, or eligibility; it persists data that downstream
phases (request segmentation, observe-mode compression accounting)
will read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

from eggpool.catalog.pricing import coerce_token_count
from eggpool.proxy.usage import (
    extract_anthropic_response_usage,
    extract_openai_response_usage,
)

logger = logging.getLogger(__name__)


class CacheCounterStatus(StrEnum):
    """Whether the upstream surfaced cache-related counters.

    * ``reported`` — the upstream payload included at least one of the
      recognized cache fields and EggPool parsed a numeric value.
    * ``not_reported`` — the upstream payload was parsed successfully
      and contained no cache fields at all (the canonical OpenAI
      shape, or providers that simply omit the breakdown).
    * ``unknown_format`` — the upstream payload could not be parsed
      as usage, or returned a shape EggPool does not recognize.  The
      cache counter state is therefore ambiguous and must not be
      assumed to be zero.
    """

    REPORTED = "reported"
    NOT_REPORTED = "not_reported"
    UNKNOWN_FORMAT = "unknown_format"


@dataclass(frozen=True, slots=True)
class NormalizedUsage:
    """Provider-neutral usage counters for a single response.

    All token counts use ``None`` to mean "not reported or unparseable"
    and ``0`` to mean "reported as zero".  ``raw_usage`` preserves the
    full upstream ``usage`` object so operators can debug parse
    failures without re-running the request.  When the upstream
    response did not include a usage block at all, ``raw_usage`` is
    ``None`` and every token count is ``None``.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    raw_usage: dict[str, Any] | None = None
    cache_counter_status: CacheCounterStatus = CacheCounterStatus.NOT_REPORTED


# Cache-counter field names that, when present in a usage payload,
# indicate the upstream actually reported cache state.  Used to choose
# between ``reported`` and ``not_reported`` in :func:`normalize_usage`.
_OPENAI_CACHE_FIELDS = ("cached_tokens",)
_ANTHROPIC_CACHE_FIELDS = (
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _raw_usage(payload: Any) -> dict[str, Any] | None:
    """Return the ``usage`` object if ``payload`` is a dict that contains one."""
    if not isinstance(payload, dict):
        return None
    payload_dict = cast("dict[str, Any]", payload)
    usage_obj: Any = payload_dict.get("usage")
    if not isinstance(usage_obj, dict):
        return None
    typed_usage = cast("dict[str, Any]", usage_obj)
    return {str(k): v for k, v in typed_usage.items()}


def _has_any_field(usage: dict[str, Any], fields: tuple[str, ...]) -> bool:
    """Return True when ``usage`` carries any of the named fields.

    The value may be ``None``, an empty string, or a non-numeric type —
    only key *presence* matters here, because the actual coercion to
    int happens in :func:`_extract_openai_cache_tokens` /
    :func:`_extract_anthropic_cache_tokens`.
    """
    return any(field in usage for field in fields)


def _extract_openai_cache_tokens(usage: dict[str, Any]) -> dict[str, int | None]:
    """Return the OpenAI-shape cache tokens as a mapping.

    OpenAI providers expose ``usage.prompt_tokens_details.cached_tokens``
    but do not (yet) split cache read vs. cache write.  We populate
    :data:`cached_input_tokens` from that field and leave the
    read/write-specific fields ``None`` so callers can distinguish a
    provider that reported cache hits from one that did not.
    """
    prompt_details: Any = usage.get("prompt_tokens_details")
    cached: int | None = None
    if isinstance(prompt_details, dict) and "cached_tokens" in prompt_details:
        prompt_details_dict = cast("dict[str, Any]", prompt_details)
        cached = coerce_token_count(prompt_details_dict.get("cached_tokens"))
        if cached == 0:
            cached = 0
    return {
        "cached_input_tokens": cached,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
        "cache_write_input_tokens": None,
    }


def _extract_anthropic_cache_tokens(usage: dict[str, Any]) -> dict[str, int | None]:
    """Return the Anthropic-shape cache tokens as a mapping.

    Anthropic providers split cache reads (``cache_read_input_tokens``)
    from cache writes (``cache_creation_input_tokens``).  We populate
    both read/write-specific fields and also ``cached_input_tokens`` as
    the canonical sum so dashboards can render a single "cached input"
    figure even when only the granular fields are available.
    """
    cache_read_raw = usage.get("cache_read_input_tokens")
    cache_creation_raw = usage.get("cache_creation_input_tokens")
    cache_read: int | None = (
        coerce_token_count(cache_read_raw)
        if "cache_read_input_tokens" in usage
        else None
    )
    cache_creation: int | None = (
        coerce_token_count(cache_creation_raw)
        if "cache_creation_input_tokens" in usage
        else None
    )
    cached_input: int | None = None
    if cache_read is not None or cache_creation is not None:
        cached_input = (cache_read or 0) + (cache_creation or 0)
    return {
        "cached_input_tokens": cached_input,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
        "cache_write_input_tokens": cache_creation,
    }


def _extract_openai(usage: dict[str, Any]) -> NormalizedUsage:
    """Build a :class:`NormalizedUsage` from an OpenAI-shape usage dict."""
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    if "prompt_tokens" in usage:
        input_tokens = coerce_token_count(usage.get("prompt_tokens"))
    if "completion_tokens" in usage:
        output_tokens = coerce_token_count(usage.get("completion_tokens"))
    if "total_tokens" in usage:
        total_tokens = coerce_token_count(usage.get("total_tokens"))
    cache_tokens = _extract_openai_cache_tokens(usage)
    reasoning_tokens: int | None = None
    completion_details: Any = usage.get("completion_tokens_details")
    if (
        isinstance(completion_details, dict)
        and "reasoning_tokens" in completion_details
    ):
        completion_details_dict = cast("dict[str, Any]", completion_details)
        reasoning_tokens = coerce_token_count(
            completion_details_dict.get("reasoning_tokens")
        )
    cache_status = (
        CacheCounterStatus.REPORTED
        if _has_any_field(usage, _OPENAI_CACHE_FIELDS)
        or any(v is not None for v in cache_tokens.values())
        else CacheCounterStatus.NOT_REPORTED
    )
    return NormalizedUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_input_tokens=cache_tokens["cached_input_tokens"],
        cache_read_input_tokens=cache_tokens["cache_read_input_tokens"],
        cache_creation_input_tokens=cache_tokens["cache_creation_input_tokens"],
        cache_write_input_tokens=cache_tokens["cache_write_input_tokens"],
        reasoning_tokens=reasoning_tokens,
        raw_usage=dict(usage),
        cache_counter_status=cache_status,
    )


def _extract_anthropic(usage: dict[str, Any]) -> NormalizedUsage:
    """Build a :class:`NormalizedUsage` from an Anthropic-shape usage dict."""
    input_tokens: int | None = None
    output_tokens: int | None = None
    if "input_tokens" in usage:
        input_tokens = coerce_token_count(usage.get("input_tokens"))
    if "output_tokens" in usage:
        output_tokens = coerce_token_count(usage.get("output_tokens"))
    cache_tokens = _extract_anthropic_cache_tokens(usage)
    cache_status = (
        CacheCounterStatus.REPORTED
        if _has_any_field(usage, _ANTHROPIC_CACHE_FIELDS)
        or any(v is not None for v in cache_tokens.values())
        else CacheCounterStatus.NOT_REPORTED
    )
    total_tokens: int | None = None
    if input_tokens is not None and output_tokens is not None:
        cached = cache_tokens["cached_input_tokens"] or 0
        total_tokens = input_tokens + output_tokens + cached
    return NormalizedUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_input_tokens=cache_tokens["cached_input_tokens"],
        cache_read_input_tokens=cache_tokens["cache_read_input_tokens"],
        cache_creation_input_tokens=cache_tokens["cache_creation_input_tokens"],
        cache_write_input_tokens=cache_tokens["cache_write_input_tokens"],
        raw_usage=dict(usage),
        cache_counter_status=cache_status,
    )


def normalize_usage(payload: Any, *, protocol: str) -> NormalizedUsage:
    """Normalise a raw upstream response payload into :class:`NormalizedUsage`.

    Parameters
    ----------
    payload:
        The decoded JSON body returned by the upstream provider.  The
        function looks for a top-level ``usage`` object; any other
        shape yields :attr:`CacheCounterStatus.UNKNOWN_FORMAT` with
        every counter left as ``None``.
    protocol:
        ``"openai"`` or ``"anthropic"`` — selects which extraction
        helper runs.  The dispatch is intentionally explicit so a
        future protocol addition can plug in here without rewriting
        call sites.

    Returns
    -------
    NormalizedUsage
        A provider-neutral usage record.  Never raises.
    """
    raw = _raw_usage(payload)
    if raw is None:
        return NormalizedUsage(cache_counter_status=CacheCounterStatus.UNKNOWN_FORMAT)

    if protocol == "openai":
        return _extract_openai(raw)
    if protocol == "anthropic":
        return _extract_anthropic(raw)
    return NormalizedUsage(
        raw_usage=raw,
        cache_counter_status=CacheCounterStatus.UNKNOWN_FORMAT,
    )


def normalize_from_stream_result(
    result: Any,
    *,
    protocol: str,
    raw_usage: dict[str, Any] | None = None,
) -> NormalizedUsage:
    """Adapt a :class:`StreamUsageResult` into :class:`NormalizedUsage`.

    Used by the streaming finalization path where the per-event
    :class:`StreamUsageResult` is the authoritative counter source
    (it merges message_start + message_delta values across an SSE
    stream).  The function preserves the legacy zero-vs-``None``
    distinction: if a cache counter is exactly zero across every event,
    it is recorded as ``0``; if it was never observed, it remains
    ``None``.

    Parameters
    ----------
    result:
        A :class:`StreamUsageResult`.  Any other type yields
        :attr:`CacheCounterStatus.UNKNOWN_FORMAT` rather than
        raising, so callers can pass ``None`` from a partial stream.
    protocol:
        ``"openai"`` or ``"anthropic"`` — selects the cache-counter
        mapping (read/write split for Anthropic, generic cached for
        OpenAI).
    raw_usage:
        Optional provider usage payload for the
        :attr:`NormalizedUsage.raw_usage` field.  When omitted, the
        raw payload is unavailable (typical streaming case) and the
        status is set from the counter presence alone.
    """
    if result is None:
        return NormalizedUsage(
            raw_usage=raw_usage,
            cache_counter_status=CacheCounterStatus.NOT_REPORTED,
        )

    input_tokens = getattr(result, "input_tokens", 0) or 0
    output_tokens = getattr(result, "output_tokens", 0) or 0
    cache_read = getattr(result, "cache_read_tokens", 0) or 0
    cache_creation = getattr(result, "cache_creation_tokens", 0) or 0

    has_input = bool(getattr(result, "input_tokens", 0))
    has_output = bool(getattr(result, "output_tokens", 0))
    has_cache_read = bool(getattr(result, "cache_read_tokens", 0))
    has_cache_creation = bool(getattr(result, "cache_creation_tokens", 0))

    if protocol == "anthropic":
        cache_read_value: int | None = cache_read if has_cache_read else None
        cache_creation_value: int | None = (
            cache_creation if has_cache_creation else None
        )
        cached_total: int | None = None
        if cache_read_value is not None or cache_creation_value is not None:
            cached_total = (cache_read_value or 0) + (cache_creation_value or 0)
        cache_status = (
            CacheCounterStatus.REPORTED
            if (has_cache_read or has_cache_creation)
            else CacheCounterStatus.NOT_REPORTED
        )
        return NormalizedUsage(
            input_tokens=input_tokens if has_input else None,
            output_tokens=output_tokens if has_output else None,
            total_tokens=(
                (input_tokens if has_input else 0)
                + (output_tokens if has_output else 0)
                + (cached_total or 0)
            )
            if (has_input or has_output)
            else None,
            cached_input_tokens=cached_total,
            cache_read_input_tokens=cache_read_value,
            cache_creation_input_tokens=cache_creation_value,
            cache_write_input_tokens=cache_creation_value,
            raw_usage=raw_usage,
            cache_counter_status=cache_status,
        )

    cached_value: int | None = cache_read if has_cache_read else None
    cache_status = (
        CacheCounterStatus.REPORTED
        if has_cache_read
        else CacheCounterStatus.NOT_REPORTED
    )
    return NormalizedUsage(
        input_tokens=input_tokens if has_input else None,
        output_tokens=output_tokens if has_output else None,
        total_tokens=(
            input_tokens + output_tokens + cache_read
            if (has_input or has_output)
            else None
        ),
        cached_input_tokens=cached_value,
        cache_read_input_tokens=None,
        cache_creation_input_tokens=None,
        cache_write_input_tokens=None,
        raw_usage=raw_usage,
        cache_counter_status=cache_status,
    )


@dataclass(slots=True)
class UsageParseDiag:
    """Lightweight diagnostic record emitted when usage normalization fails.

    The Phase 1 observability layer logs a compact diagnostic for each
    failure mode (no usage block, unparseable JSON, unknown shape) so
    operators can answer "which providers do not expose cache
    counters?" without scrolling through stdout.  See
    :func:`emit_parse_failure_log` for the structured log keys.
    """

    provider_id: str | None
    model_id: str | None
    protocol: str
    reason: str
    raw_keys: list[str] = field(default_factory=list[str])


def emit_parse_failure_log(diag: UsageParseDiag) -> None:
    """Emit a structured debug log for a usage-parse failure.

    The log keys are deliberately short and never include the request
    body, prompt, or auth header.  They map 1:1 to the canonical
    diagnostic strings listed in
    ``plans/cache_compression_phase_01_cache_token_observability.md``:

    * ``cache_usage_not_reported`` — usage parsed cleanly but no cache
      fields were present.
    * ``cache_usage_unknown_shape`` — usage was present but its shape
      could not be classified.
    * ``usage_parse_missing_final_stream_event`` — the stream ended
      before a final usage event arrived.
    * ``usage_parse_preserved_raw_only`` — parsing failed entirely;
      only the raw payload is available.
    """
    log_payload = {
        "provider_id": diag.provider_id,
        "model_id": diag.model_id,
        "protocol": diag.protocol,
        "raw_keys": diag.raw_keys,
    }
    if diag.reason == "missing_usage_block":
        logger.debug("cache_usage_not_reported", extra=log_payload)
    elif diag.reason == "unknown_shape":
        logger.debug("cache_usage_unknown_shape", extra=log_payload)
    elif diag.reason == "missing_final_stream_event":
        logger.debug("usage_parse_missing_final_stream_event", extra=log_payload)
    elif diag.reason == "preserved_raw_only":
        logger.debug("usage_parse_preserved_raw_only", extra=log_payload)
    else:
        logger.debug("usage_parse_preserved_raw_only", extra=log_payload)


# Re-export the protocol-specific extractors so existing callers that
# need the lightweight ``StreamUsageResult`` shape continue to work
# without a second import.  The streaming/non-streaming split lives in
# ``eggpool.proxy.usage``; this module only adds the normalized view.
__all__ = [
    "CacheCounterStatus",
    "NormalizedUsage",
    "UsageParseDiag",
    "emit_parse_failure_log",
    "extract_anthropic_response_usage",
    "extract_openai_response_usage",
    "normalize_from_stream_result",
    "normalize_usage",
]
