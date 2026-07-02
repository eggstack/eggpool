"""Observe-mode deterministic compression accounting (Phase 4).

This subpackage implements Phase 4 of the cache-preserving
deterministic compression roadmap.  See
``plans/cache_compression_phase_04_observe_mode_compression_accounting.md``
for the full design.

Public surface:

- :class:`CompressionConfig` (typed config in ``policy.py``)
- :func:`analyze_compression` and :class:`CompressionObservation`
  (analyzer in ``analyzer.py``)

The analyzer is observational: it records what a future phase
would compress but never mutates the request body, never
changes routing, and never synthesises provider cache controls.
"""

from __future__ import annotations

from eggpool.transcoder.compression.analyzer import (
    REASON_BASE64_ELISION,
    REASON_BELOW_MIN_CANDIDATE_TOKENS,
    REASON_BELOW_MIN_SAVINGS_TOKENS,
    REASON_EMPTY_SEGMENT,
    REASON_JSON_MINIFY,
    REASON_LATENCY_BUDGET,
    REASON_LOG_COMPACTION,
    REASON_PLACEMENT,
    REASON_PROTECTED_CACHE_BOUNDARY,
    REASON_REPEATED_LINE_RUN,
    REASON_SEARCH_COMPACTION,
    REASON_STACK_TRACE_COMPACTION,
    REASON_STATIC_PREFIX,
    REASON_TRANSFORM_DISABLED,
    CompressionCandidate,
    CompressionObservation,
    TransformLiteral,
    analyze_compression,
)
from eggpool.transcoder.compression.policy import (
    CompressionConfig,
    CompressionMode,
    CompressionPlacement,
    CompressionTransforms,
)

__all__ = [
    "CompressionCandidate",
    "CompressionConfig",
    "CompressionMode",
    "CompressionObservation",
    "CompressionPlacement",
    "CompressionTransforms",
    "REASON_BELOW_MIN_CANDIDATE_TOKENS",
    "REASON_BELOW_MIN_SAVINGS_TOKENS",
    "REASON_BASE64_ELISION",
    "REASON_EMPTY_SEGMENT",
    "REASON_JSON_MINIFY",
    "REASON_LATENCY_BUDGET",
    "REASON_LOG_COMPACTION",
    "REASON_PLACEMENT",
    "REASON_PROTECTED_CACHE_BOUNDARY",
    "REASON_REPEATED_LINE_RUN",
    "REASON_SEARCH_COMPACTION",
    "REASON_STACK_TRACE_COMPACTION",
    "REASON_STATIC_PREFIX",
    "REASON_TRANSFORM_DISABLED",
    "TransformLiteral",
    "analyze_compression",
]
