"""Compression subpackage (Phase 4 + Phase 5 + Phase 6).

This subpackage implements Phase 4 (observe-mode accounting), Phase 5
(safe-mode deterministic compression), and Phase 6 (operator-controllable
policy overrides) of the cache-preserving deterministic compression
roadmap.

Public surface:

- :class:`CompressionConfig` (typed config in ``policy.py``)
- :class:`CompressionPolicyOverride` (Phase 6 typed override row in ``policy.py``)
- :func:`resolve_compression_policy` and :class:`ResolvedCompressionPolicy`
  (Phase 6 deterministic resolver in ``policy_resolver.py``)
- :class:`CompressionPolicyContext` (Phase 6 input context)
- :func:`analyze_compression` and :class:`CompressionObservation`
  (analyzer in ``analyzer.py``)
- :func:`apply_safe_compression` and :class:`CompressionResult`
  (safe-mode applier in ``apply.py``)
- :func:`build_marker`, :func:`parse_marker`, :func:`is_marker_line`,
  :class:`MarkerLine` (deterministic markers in ``markers.py``)

The analyzer is observational: it records what a future phase
would compress but never mutates the request body, never
changes routing, and never synthesises provider cache controls.
The safe-mode applier mutates only eligible volatile_suffix
segments on a deep-copied payload and never touches stable
prefixes or cache-protected blocks.  The Phase 6 resolver is
content-private: it never inspects prompt bodies, model output,
or any other request payload fields; it only matches on the
client identity, source/target protocol, requested/resolved
model, provider id/kind, and the transcoded flag.
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
from eggpool.transcoder.compression.apply import (
    REASON_PREFIX_HASH_MISMATCH,
    CompressionResult,
    apply_safe_compression,
    result_to_summary,
)
from eggpool.transcoder.compression.markers import (
    MarkerLine,
    build_marker,
    is_marker_line,
    parse_marker,
)
from eggpool.transcoder.compression.policy import (
    CompressionConfig,
    CompressionMode,
    CompressionPlacement,
    CompressionPolicyOverride,
    CompressionProtocolMatch,
    CompressionTransforms,
)
from eggpool.transcoder.compression.policy_resolver import (
    CompressionPolicyContext,
    ResolvedCompressionPolicy,
    resolve_compression_policy,
)

__all__ = [
    "CompressionCandidate",
    "CompressionConfig",
    "CompressionMode",
    "CompressionObservation",
    "CompressionPlacement",
    "CompressionPolicyContext",
    "CompressionPolicyOverride",
    "CompressionProtocolMatch",
    "CompressionResult",
    "CompressionTransforms",
    "MarkerLine",
    "REASON_BASE64_ELISION",
    "REASON_BELOW_MIN_CANDIDATE_TOKENS",
    "REASON_BELOW_MIN_SAVINGS_TOKENS",
    "REASON_EMPTY_SEGMENT",
    "REASON_JSON_MINIFY",
    "REASON_LATENCY_BUDGET",
    "REASON_LOG_COMPACTION",
    "REASON_PLACEMENT",
    "REASON_PREFIX_HASH_MISMATCH",
    "REASON_PROTECTED_CACHE_BOUNDARY",
    "REASON_REPEATED_LINE_RUN",
    "REASON_SEARCH_COMPACTION",
    "REASON_STACK_TRACE_COMPACTION",
    "REASON_STATIC_PREFIX",
    "REASON_TRANSFORM_DISABLED",
    "ResolvedCompressionPolicy",
    "TransformLiteral",
    "analyze_compression",
    "apply_safe_compression",
    "build_marker",
    "is_marker_line",
    "parse_marker",
    "resolve_compression_policy",
    "result_to_summary",
]
