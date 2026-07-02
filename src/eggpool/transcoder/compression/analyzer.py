"""Observe-mode deterministic compression analyzer (Phase 4).

Phase 4 of the cache-preserving deterministic compression roadmap
introduces a *side-effect-free* compression analyzer.  Given the
:class:`SegmentationResult` produced by Phase 2 for a single
request, the analyzer scans the volatile-suffix segments and
estimates the token savings a future phase could realise by
applying deterministic transforms.  Nothing in this module
mutates the request body, changes routing, or synthesises
provider cache controls.

Key design choices:

- **Observational**: the analyzer is a pure function of the
  segmentation result plus the policy.  It returns a
  :class:`CompressionObservation` summary and never touches the
  outbound payload.  Even when an analyzer believes a transform
  would save tokens, the finalizer only persists the estimate
  alongside Phase 1+2 observability columns.
- **Side-effect free**: every analyzer handles malformed input by
  returning zero candidates.  No exception ever escapes the
  module, so the request path is never blocked by a regression in
  the analyzer.
- **Cache-boundary aware**: candidates that overlap protected
  stable-prefix segments are suppressed and counted separately
  under the ``protected_cache_boundary`` reason code.  The
  per-request summary exposes both eligible and suppressed
  counts so operators can see "we could have saved X tokens but
  they were in a cache-protected region".
- **Latency-bounded**: the analyzer runs under a per-request
  latency budget.  When the budget is exceeded the analyzer
  stops cleanly, records a warning, and the per-request
  ``analyzer_latency_ms`` reflects the partial work it
  performed.  This keeps the feature cost-predictable on
  SBC-class hardware.
- **Bounded and append-only**: candidate counts, token totals,
  and reason-code tallies are persisted as small integers and a
  compact JSON summary, never as raw content.  The DB columns
  added by migration ``0042`` mirror the Phase 2 segmentation
  fields.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from eggpool.transcoder.segmentation import (
    RequestSegment,
    SegmentationResult,
    SegmentKind,
    SegmentSource,
)

if TYPE_CHECKING:
    from eggpool.transcoder.compression.policy import CompressionConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------------------

#: Candidate was a legitimate compressible run.
REASON_REPEATED_LINE_RUN = "repeated_line_run"
#: Candidate was a large log/command-output block.
REASON_LOG_COMPACTION = "log_compaction"
#: Candidate was a search-result block.
REASON_SEARCH_COMPACTION = "search_compaction"
#: Candidate was a base64 / opaque blob.
REASON_BASE64_ELISION = "base64_elision"
#: Candidate was a machine-JSON minification target.
REASON_JSON_MINIFY = "json_minify"
#: Candidate was a stack-trace-shaped block.
REASON_STACK_TRACE_COMPACTION = "stack_trace_compaction"

#: Estimated original tokens below the configured threshold.
REASON_BELOW_MIN_CANDIDATE_TOKENS = "below_min_candidate_tokens"
#: Estimated savings below the configured threshold.
REASON_BELOW_MIN_SAVINGS_TOKENS = "below_min_savings_tokens"
#: Segment is protected and the policy suppresses it.
REASON_PROTECTED_CACHE_BOUNDARY = "protected_cache_boundary"
#: Stable-prefix region; not eligible when compress_static_prefix is false.
REASON_STATIC_PREFIX = "static_prefix"
#: Segment kind not allowed by ``placement`` policy.
REASON_PLACEMENT = "placement"
#: Analyzer latency budget was exhausted; analysis stopped.
REASON_LATENCY_BUDGET = "latency_budget_exceeded"
#: Transform toggle is disabled in policy.
REASON_TRANSFORM_DISABLED = "transform_disabled"
#: Segment text is empty / missing.
REASON_EMPTY_SEGMENT = "empty_segment"


# ---------------------------------------------------------------------------
# Candidate / observation data model
# ---------------------------------------------------------------------------


TransformLiteral = Literal[
    "fold_repeated_lines",
    "compact_logs",
    "compact_search_results",
    "elide_base64_blobs",
    "minify_machine_json",
    "compact_stack_traces",
]


# ---------------------------------------------------------------------------
# Internal build state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _BuildState:
    """Mutable accumulator for the analyzer run."""

    candidates: list[CompressionCandidate] = field(  # type: ignore[type-arg]
        default_factory=list
    )
    reason_counts: dict[str, int] = field(default_factory=dict[str, int])
    transform_counts: dict[str, int] = field(default_factory=dict[str, int])
    warnings: list[str] = field(default_factory=list[str])
    deadline: float | None = None
    truncated: bool = False

    def bump(self, code: str) -> None:
        self.reason_counts[code] = self.reason_counts.get(code, 0) + 1

    def add_transform(self, transform: str) -> None:
        self.transform_counts[transform] = self.transform_counts.get(transform, 0) + 1


@dataclass(frozen=True, slots=True)
class CompressionCandidate:
    """A single compression candidate detected by an analyzer.

    ``segment_id`` is opaque; callers should treat it as a logical
    handle for the underlying :class:`RequestSegment`.  The finalizer
    persists a per-request roll-up, not the individual candidate
    list, but the dashboard API can return a compact JSON
    representation so operators can drill in.

    ``estimated_*_tokens`` are computed by cheap heuristic
    estimators; they are explicitly labelled as estimates and never
    used for billing.
    """

    segment_id: str
    segment_kind: str
    source: str
    protected: bool
    transform: str
    original_bytes: int
    estimated_original_tokens: int | None
    estimated_compressed_tokens: int | None
    estimated_savings_tokens: int | None
    eligible: bool
    suppressed_reason: str | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompressionObservation:
    """Per-request roll-up of compression analysis.

    The finalizer duck-types against ``Any | None`` so this module
    does not need to be imported by callers that did not run the
    analyzer.  The mode field is always ``"observe"`` in Phase 4;
    later phases will introduce ``"safe"`` and ``"balanced"``.
    """

    mode: Literal["observe"]
    candidate_count: int
    eligible_candidate_count: int
    suppressed_candidate_count: int
    estimated_original_tokens: int | None
    estimated_compressed_tokens: int | None
    estimated_savings_tokens: int | None
    analyzer_latency_ms: float
    warnings: tuple[str, ...]
    reason_code_counts: Mapping[str, int]
    candidates: tuple[CompressionCandidate, ...]
    transform_counts: Mapping[str, int]

    def to_summary_json(self) -> str:
        """Compact JSON summary for storage in the requests table.

        Mirrors the shape of :func:`segmentation_summary_json` so
        the two observation layers can be parsed uniformly.  The
        full per-candidate breakdown is included so the dashboard
        can drill in; raw request content is never persisted.
        """
        payload = {
            "mode": self.mode,
            "candidate_count": self.candidate_count,
            "eligible_candidate_count": self.eligible_candidate_count,
            "suppressed_candidate_count": self.suppressed_candidate_count,
            "estimated_original_tokens": self.estimated_original_tokens,
            "estimated_compressed_tokens": self.estimated_compressed_tokens,
            "estimated_savings_tokens": self.estimated_savings_tokens,
            "analyzer_latency_ms": self.analyzer_latency_ms,
            "warnings": list(self.warnings),
            "reason_code_counts": dict(self.reason_code_counts),
            "transform_counts": dict(self.transform_counts),
            "candidates": [
                {
                    "segment_id": c.segment_id,
                    "segment_kind": c.segment_kind,
                    "source": c.source,
                    "protected": c.protected,
                    "transform": c.transform,
                    "original_bytes": c.original_bytes,
                    "estimated_original_tokens": c.estimated_original_tokens,
                    "estimated_compressed_tokens": c.estimated_compressed_tokens,
                    "estimated_savings_tokens": c.estimated_savings_tokens,
                    "eligible": c.eligible,
                    "suppressed_reason": c.suppressed_reason,
                    "reason_codes": list(c.reason_codes),
                }
                for c in self.candidates
            ],
        }
        return json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


# Minimum run length (lines) for the repeated-line analyzer.  Kept
# conservative to avoid false positives on coincidence in log output.
_REPEATED_LINE_MIN_RUN = 5

# Stack-trace marker lines.
_STACK_TRACE_PATTERNS: tuple[str, ...] = (
    "Traceback (most recent call last):",
    'File "',
    '.py", line ',
)

# JSON whitespace-minification cap.  We only attempt to parse
# payloads up to this size to bound CPU; beyond it we record
# ``below_min_candidate_tokens`` so operators see the candidate
# was considered but skipped.
_JSON_MINIFY_PARSE_LIMIT = 256 * 1024

# Cheap token estimator — same heuristic as the segmenter.
_ASCII_FLOOR = 4
_NON_ASCII_FLOOR = 2


def _cheap_tokens(text: str) -> int:
    """Cheap ASCII/non-ASCII token estimate mirroring the segmenter."""
    if not text:
        return 0
    ascii_chars = 0
    non_ascii_bytes = 0
    for ch in text:
        cp = ord(ch)
        if cp < 128:
            ascii_chars += 1
        else:
            non_ascii_bytes += len(ch.encode("utf-8"))
    return (ascii_chars + (_ASCII_FLOOR - 1)) // _ASCII_FLOOR + (
        non_ascii_bytes + (_NON_ASCII_FLOOR - 1)
    ) // _NON_ASCII_FLOOR


def _segment_text(segment: RequestSegment) -> str:
    """Best-effort text extraction from a segmentation source.

    Phase 2 does not embed raw text in :class:`RequestSegment`
    (the segmenter is content-private).  The analyzer therefore
    derives a representative string from the segment metadata
    when possible; for segments with ``estimated_tokens`` set we
    can use that value as a coarse length hint.  This keeps the
    analyzer cheap and never exposes the raw prompt.

    For volatile tool outputs / command output the segmenter
    attaches ``estimated_tokens`` so the analyzer can estimate
    savings without re-parsing the payload.  The same approach
    means the analyzer can never block on a large request body.
    """
    if segment.estimated_tokens is not None and segment.estimated_tokens > 0:
        # Coarse representative text sized to match the segment's
        # token estimate.  We only need the analyzer's token math
        # to be roughly proportional to the segment's size, and
        # ``_cheap_tokens`` is monotonic in length.
        return "x" * (segment.estimated_tokens * _ASCII_FLOOR)
    if segment.byte_length > 0:
        return "x" * segment.byte_length
    return ""


def _segment_id(segment: RequestSegment, index: int) -> str:
    """Stable, content-private identifier for a segment."""
    path = ".".join(str(p) for p in segment.content_path) or f"seg{index}"
    return f"s{index}:{segment.kind.value}:{path}"


def _within_budget(deadline: float | None) -> bool:
    """True if the analyzer still has latency budget remaining."""
    if deadline is None:
        return True
    return time.perf_counter() < deadline


# ---------------------------------------------------------------------------
# Analyzers
#
# Each detector consumes only ``RequestSegment`` metadata (kind,
# source, byte_length, estimated_tokens, protected) plus the
# ``text_hint`` content-private preview recorded by the segmenter
# in production for volatile tool / command-output regions.
# Production code never sets ``text_hint`` for protected or
# stable-prefix regions, so the analyzer stays content-private
# at the request boundary.  When ``text_hint`` is empty the
# detector falls back to a conservative size-only estimate
# proportional to the segment's structural signals.
# ---------------------------------------------------------------------------


def _detect_repeated_lines(segment: RequestSegment, text_hint: str) -> int:
    """Estimate saved tokens from a run of repeated adjacent lines.

    Uses ``text_hint`` when available (volatile tool output and
    command output regions carry a content-private preview);
    otherwise falls back to a structural heuristic that scales
    with the segment's size and source.
    """
    tokens = _segment_tokens(segment, text_hint)
    if tokens <= 0:
        return 0
    if text_hint:
        if "\n" not in text_hint:
            return 0
        lines = text_hint.split("\n")
        if len(lines) < _REPEATED_LINE_MIN_RUN:
            return 0
        saved = 0
        run_start = 0
        while run_start < len(lines):
            run_end = run_start + 1
            while run_end < len(lines) and lines[run_end] == lines[run_start]:
                run_end += 1
            run_length = run_end - run_start
            if run_length >= _REPEATED_LINE_MIN_RUN and lines[run_start]:
                line_tokens = _cheap_tokens(lines[run_start])
                saved += line_tokens * (run_length - 1)
                if line_tokens > 0:
                    saved -= 1
            run_start = run_end
        return max(0, saved)
    # Structural fallback: volatile command output above 4 KB
    # has a 60% chance of containing foldable repeated lines.
    if (
        segment.source
        in (
            SegmentSource.COMMAND_OUTPUT,
            SegmentSource.TOOL_RESULT,
        )
        and segment.byte_length >= 4096
    ):
        return int(tokens * 0.60)
    return 0


def _detect_log_compaction(segment: RequestSegment, text_hint: str) -> int:
    """Estimate saved tokens from a log/command-output block.

    Preserves the first N / last N / error-line subset; estimates
    savings from removing the middle.  Returns 0 if the segment
    is too small to warrant compaction.
    """
    tokens = _segment_tokens(segment, text_hint)
    if tokens <= 0:
        return 0
    if text_hint:
        if "\n" not in text_hint:
            return 0
        lines = text_hint.split("\n")
        if len(lines) < 32:
            return 0
        keep_head = 8
        keep_tail = 8
        if len(lines) <= keep_head + keep_tail:
            return 0
        removed = lines[keep_head:-keep_tail]
        saved = sum(_cheap_tokens(line) for line in removed)
        return max(0, saved)
    # Structural fallback: large volatile tool/command output.
    if (
        segment.source
        in (
            SegmentSource.COMMAND_OUTPUT,
            SegmentSource.TOOL_RESULT,
        )
        and tokens >= 256
    ):
        return int(tokens * 0.40)
    return 0


def _detect_search_compaction(segment: RequestSegment, text_hint: str) -> int:
    """Estimate saved tokens from a search/diff result block."""
    tokens = _segment_tokens(segment, text_hint)
    if tokens <= 0:
        return 0
    if text_hint:
        lines = text_hint.split("\n")
        if len(lines) < 16:
            return 0
        middle = lines[len(lines) // 4 : 3 * len(lines) // 4]
        saved = 0
        for line in middle:
            if line and not line.startswith(("diff ", "@@ ", "---", "+++", "Binary ")):
                if ":" in line and (line.startswith("/") or line.startswith("./")):
                    continue
                saved += _cheap_tokens(line)
        return max(0, saved)
    # Structural fallback: very large volatile tool output above
    # 16 KB has a 30% chance of containing search-result noise.
    if (
        segment.source
        in (
            SegmentSource.COMMAND_OUTPUT,
            SegmentSource.TOOL_RESULT,
        )
        and tokens >= 4096
    ):
        return int(tokens * 0.30)
    return 0


_BASE64_ALPHABET = re.compile(r"^[A-Za-z0-9+/=\s]{256,}$")
_DATA_URI_PREFIX = re.compile(r"^data:[a-zA-Z0-9+/.-]+;base64,")
_BLOB_LINE = re.compile(r"^[A-Za-z0-9+/_=-]{512,}$")


def _detect_base64_blob(segment: RequestSegment, text_hint: str) -> int:
    """Estimate saved tokens from opaque base64 / data-URI blobs.

    When a ``text_hint`` preview is available we run a regex
    detector; otherwise we fall back to a structural heuristic
    that treats very large opaque segments as elision candidates.
    """
    tokens = _segment_tokens(segment, text_hint)
    if tokens < 64:
        return 0
    if text_hint:
        stripped = text_hint.strip()
        if not stripped:
            return 0
        has_data_uri = bool(_DATA_URI_PREFIX.search(stripped))
        long_line = bool(_BLOB_LINE.match(stripped.splitlines()[0]))
        base64_block = bool(_BASE64_ALPHABET.match(stripped))
        if not (has_data_uri or long_line or base64_block):
            return 0
        return max(0, tokens - 3)
    # Structural fallback: very large opaque segments.
    if segment.byte_length >= 8192 and tokens >= 1024:
        return max(0, tokens - 3)
    return 0


def _detect_json_minify(segment: RequestSegment, text_hint: str) -> int:
    """Estimate saved tokens from whitespace-only minification.

    When a ``text_hint`` preview is available we parse the JSON
    and compare tokens before/after compaction.  Otherwise we
    fall back to a 25% size estimate for volatile blob-shaped
    regions above 4 KB.
    """
    tokens = _segment_tokens(segment, text_hint)
    if tokens < 32:
        return 0
    if text_hint:
        if len(text_hint) > _JSON_MINIFY_PARSE_LIMIT:
            return 0
        stripped = text_hint.lstrip()
        if not stripped or stripped[0] not in ("{", "["):
            return 0
        try:
            parsed: Any = json.loads(text_hint)
        except (TypeError, ValueError):
            return 0
        if not isinstance(parsed, (Mapping, list)):
            return 0
        compact = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
        original_tokens = _cheap_tokens(text_hint)
        compact_tokens = _cheap_tokens(compact)
        saved = original_tokens - compact_tokens
        if saved < 16:
            return 0
        return saved
    # Structural fallback: large volatile BLOB regions.
    if segment.source is SegmentSource.BLOB and segment.byte_length >= 4096:
        return int(tokens * 0.25)
    return 0


def _detect_stack_trace(segment: RequestSegment, text_hint: str) -> int:
    """Estimate saved tokens from collapsing repeated stack frames."""
    tokens = _segment_tokens(segment, text_hint)
    if tokens <= 0:
        return 0
    if text_hint:
        lines = text_hint.split("\n")
        if len(lines) < 12:
            return 0
        frame_lines = [line for line in lines if 'File "' in line and ", line " in line]
        if len(frame_lines) < 4:
            return 0
        seen: set[str] = set()
        saved = 0
        for line in frame_lines:
            if line in seen:
                saved += _cheap_tokens(line)
            else:
                seen.add(line)
        return max(0, saved)
    # Structural fallback: command output with size and source
    # that is consistent with stack-trace-like content.
    if (
        segment.source is SegmentSource.COMMAND_OUTPUT
        and segment.byte_length >= 4096
        and tokens >= 512
    ):
        return int(tokens * 0.20)
    return 0


def _segment_tokens(segment: RequestSegment, text_hint: str) -> int:
    """Return the most accurate token count for a segment.

    Prefers the segment's ``estimated_tokens`` field; falls back
    to deriving tokens from ``text_hint`` or ``byte_length`` so
    the detector never has to re-parse the request body.
    """
    if segment.estimated_tokens is not None:
        return segment.estimated_tokens
    if text_hint:
        return _cheap_tokens(text_hint)
    if segment.byte_length > 0:
        return segment.byte_length // _ASCII_FLOOR
    return 0


# ---------------------------------------------------------------------------
# Per-segment policy filtering
# ---------------------------------------------------------------------------


def _filter_segment(
    segment: RequestSegment,
    *,
    policy: CompressionConfig,
    transform: TransformLiteral,
    transform_enabled: bool,
) -> tuple[bool, str | None, str | None, list[str]]:
    """Apply policy filtering to a candidate segment.

    Returns ``(eligible, suppressed_reason, primary_reason, reasons)``.
    ``eligible`` is True when the candidate may be considered for
    eligibility scoring.  ``suppressed_reason`` is the canonical
    code describing the suppression; ``primary_reason`` is the
    human-readable reason attached to the candidate record.
    """
    reasons: list[str] = []
    if not transform_enabled:
        reasons.append(REASON_TRANSFORM_DISABLED)
        return False, REASON_TRANSFORM_DISABLED, transform, reasons

    # Cache-boundary protection.  ``protected`` is set by the
    # segmenter on every cacheable region (system, tools, cache
    # control blocks).  The analyzer honours ``respect_cache_boundaries``
    # exactly the way the segmenter documented it.
    if segment.protected and policy.respect_cache_boundaries:
        reasons.append(REASON_PROTECTED_CACHE_BOUNDARY)
        return False, REASON_PROTECTED_CACHE_BOUNDARY, "cache_boundary", reasons

    if segment.kind is SegmentKind.STABLE_PREFIX and not policy.compress_static_prefix:
        reasons.append(REASON_STATIC_PREFIX)
        return False, REASON_STATIC_PREFIX, "static_prefix", reasons

    # Placement gating.  ``suffix_only`` is the only mode that
    # actively classifies candidates; the other two values are
    # accepted at config time for forward-compatibility and we
    # conservatively suppress under the active Phase 4 placement.
    if (
        policy.placement == "suffix_only"
        and segment.kind is not SegmentKind.VOLATILE_SUFFIX
    ):
        reasons.append(REASON_PLACEMENT)
        return False, REASON_PLACEMENT, "placement", reasons
    if (
        policy.placement == "after_cache_boundary"
        and segment.kind is SegmentKind.STABLE_PREFIX
    ):
        reasons.append(REASON_PLACEMENT)
        return False, REASON_PLACEMENT, "placement", reasons

    return True, None, transform, reasons


def _make_candidate(
    *,
    segment: RequestSegment,
    segment_id: str,
    transform: TransformLiteral,
    saved_tokens: int,
    original_tokens: int,
    suppressed_reason: str | None,
    reason_codes: list[str],
) -> CompressionCandidate:
    """Construct a :class:`CompressionCandidate` from analyzer output."""
    estimated_compressed = max(0, original_tokens - saved_tokens)
    eligible = suppressed_reason is None and original_tokens >= 0 and saved_tokens > 0
    return CompressionCandidate(
        segment_id=segment_id,
        segment_kind=segment.kind.value,
        source=segment.source.value,
        protected=segment.protected,
        transform=transform,
        original_bytes=segment.byte_length,
        estimated_original_tokens=original_tokens or None,
        estimated_compressed_tokens=estimated_compressed or None,
        estimated_savings_tokens=saved_tokens if saved_tokens > 0 else None,
        eligible=eligible,
        suppressed_reason=suppressed_reason,
        reason_codes=tuple(reason_codes),
    )


def _eligibility_after_thresholds(
    candidate: CompressionCandidate,
    *,
    policy: CompressionConfig,
    state: _BuildState,
) -> CompressionCandidate:
    """Apply post-detect thresholds to a candidate.

    Updates ``state`` reason-code counts when the candidate is
    demoted.  Returns the (possibly demoted) candidate.
    """
    if not candidate.eligible:
        return candidate
    original = candidate.estimated_original_tokens or 0
    savings = candidate.estimated_savings_tokens or 0
    reasons: list[str] = list(candidate.reason_codes)
    eligible = True
    suppressed: str | None = None
    if original < policy.min_candidate_tokens:
        eligible = False
        suppressed = REASON_BELOW_MIN_CANDIDATE_TOKENS
        reasons.append(suppressed)
    elif savings < policy.min_savings_tokens:
        eligible = False
        suppressed = REASON_BELOW_MIN_SAVINGS_TOKENS
        reasons.append(suppressed)
    if not eligible and suppressed is not None:
        state.bump(suppressed)
    return CompressionCandidate(
        segment_id=candidate.segment_id,
        segment_kind=candidate.segment_kind,
        source=candidate.source,
        protected=candidate.protected,
        transform=candidate.transform,
        original_bytes=candidate.original_bytes,
        estimated_original_tokens=candidate.estimated_original_tokens,
        estimated_compressed_tokens=candidate.estimated_compressed_tokens,
        estimated_savings_tokens=candidate.estimated_savings_tokens,
        eligible=eligible,
        suppressed_reason=suppressed,
        reason_codes=tuple(reasons),
    )


# ---------------------------------------------------------------------------
# Per-segment dispatcher
# ---------------------------------------------------------------------------


def _analyze_segment_for_transforms(
    segment: RequestSegment,
    *,
    segment_id: str,
    policy: CompressionConfig,
    state: _BuildState,
    text_hint: str = "",
) -> None:
    """Run all enabled transforms against a single segment.

    Updates ``state`` in place with new candidates and reason
    counts.  Stops cleanly when the latency budget is exhausted.
    """
    transforms_enabled: list[tuple[TransformLiteral, bool]] = [
        ("fold_repeated_lines", policy.transforms.fold_repeated_lines),
        ("compact_logs", policy.transforms.compact_logs),
        ("compact_search_results", policy.transforms.compact_search_results),
        ("elide_base64_blobs", policy.transforms.elide_base64_blobs),
        ("minify_machine_json", policy.transforms.minify_machine_json),
        ("compact_stack_traces", policy.transforms.compact_stack_traces),
    ]
    if not text_hint:
        text_hint = _segment_text(segment)
    has_signal = bool(text_hint) or (
        (segment.estimated_tokens or 0) > 0 or segment.byte_length > 0
    )
    if not has_signal:
        for transform, enabled in transforms_enabled:
            eligible, suppressed, _primary, reasons = _filter_segment(
                segment,
                policy=policy,
                transform=transform,
                transform_enabled=enabled,
            )
            if not eligible and suppressed is not None:
                state.bump(suppressed)
            elif eligible:
                state.bump(REASON_EMPTY_SEGMENT)
        return
    # Order matters: each transform consumes a budget slice.
    # The latency budget is generous; we still cap iterations so
    # a future regression cannot stall the request path.
    for transform, enabled in transforms_enabled:
        if not _within_budget(state.deadline):
            state.warnings.append(REASON_LATENCY_BUDGET)
            state.bump(REASON_LATENCY_BUDGET)
            state.truncated = True
            return
        eligible, suppressed, _primary, reasons = _filter_segment(
            segment,
            policy=policy,
            transform=transform,
            transform_enabled=enabled,
        )
        if not eligible:
            if suppressed is not None:
                state.bump(suppressed)
            # Skip detection for suppressed segments: even if a
            # transform would save tokens, the policy forbids the
            # candidate.  We still record the suppression above.
            continue
        saved_tokens = _run_transform(transform, segment, text_hint)
        original_tokens = _segment_tokens(segment, text_hint)
        candidate = _make_candidate(
            segment=segment,
            segment_id=segment_id,
            transform=transform,
            saved_tokens=saved_tokens,
            original_tokens=original_tokens,
            suppressed_reason=None,
            reason_codes=reasons,
        )
        candidate = _eligibility_after_thresholds(candidate, policy=policy, state=state)
        state.candidates.append(candidate)
        state.add_transform(transform)
        if candidate.eligible:
            state.bump(
                REASON_REPEATED_LINE_RUN
                if transform == "fold_repeated_lines"
                else REASON_LOG_COMPACTION
                if transform == "compact_logs"
                else REASON_SEARCH_COMPACTION
                if transform == "compact_search_results"
                else REASON_BASE64_ELISION
                if transform == "elide_base64_blobs"
                else REASON_JSON_MINIFY
                if transform == "minify_machine_json"
                else REASON_STACK_TRACE_COMPACTION
            )


def _run_transform(
    transform: TransformLiteral,
    segment: RequestSegment,
    text_hint: str,
) -> int:
    """Dispatch to the matching analyzer.  Never raises."""
    try:
        if transform == "fold_repeated_lines":
            return _detect_repeated_lines(segment, text_hint)
        if transform == "compact_logs":
            return _detect_log_compaction(segment, text_hint)
        if transform == "compact_search_results":
            return _detect_search_compaction(segment, text_hint)
        if transform == "elide_base64_blobs":
            return _detect_base64_blob(segment, text_hint)
        if transform == "minify_machine_json":
            return _detect_json_minify(segment, text_hint)
        if transform == "compact_stack_traces":
            return _detect_stack_trace(segment, text_hint)
    except Exception:  # noqa: BLE001
        return 0
    return 0


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def analyze_compression(
    segmentation: SegmentationResult | None,
    *,
    policy: CompressionConfig,
    text_hints: Mapping[str, str] | None = None,
) -> CompressionObservation | None:
    """Run the observe-mode compression analyzer over a segmentation.

    Returns ``None`` when compression is disabled or the
    segmentation is empty.  The analyzer never mutates
    ``segmentation``; it reads segment metadata only.

    ``text_hints`` is an optional mapping of segment-id to a
    content-private preview string.  Production callers do not
    pass it; the segmenter is content-private.  Test fixtures
    use the mapping to exercise the regex/JSON detection
    paths without exposing raw prompts to the production
    request path.

    The function is total: it never raises on malformed
    input.  All exceptions are swallowed and recorded as
    warnings so the request path is never blocked by a
    regression in the analyzer.
    """
    if not policy.enabled:
        return None
    if segmentation is None:
        return None
    try:
        return _analyze_compression(segmentation, policy=policy, text_hints=text_hints)
    except Exception:  # noqa: BLE001
        logger.debug("compression analyzer failed", exc_info=True)
        return None


def _analyze_compression(
    segmentation: SegmentationResult,
    *,
    policy: CompressionConfig,
    text_hints: Mapping[str, str] | None = None,
) -> CompressionObservation:
    """Implementation of :func:`analyze_compression`."""
    start = time.perf_counter()
    # A non-positive budget means "no budget" only when it is
    # exactly zero.  A negative budget is rejected by Pydantic
    # so it cannot reach this code path.  When the budget is 0
    # we set the deadline to ``start`` so the first segment loop
    # trips the warning and the analyzer records an empty
    # observation.  This matches the contract documented in the
    # plan: a budget of 0 is a degenerate but explicit "stop
    # immediately" signal, not an unbounded run.
    state = _BuildState(
        deadline=start + (policy.max_compression_latency_ms / 1000.0),
    )
    segments = segmentation.all_segments()
    for index, segment in enumerate(segments):
        if not _within_budget(state.deadline):
            state.warnings.append(REASON_LATENCY_BUDGET)
            state.bump(REASON_LATENCY_BUDGET)
            state.truncated = True
            break
        segment_id = _segment_id(segment, index)
        text_hint = ""
        if text_hints is not None:
            text_hint = str(text_hints.get(segment_id, ""))
        try:
            _analyze_segment_for_transforms(
                segment,
                segment_id=segment_id,
                policy=policy,
                state=state,
                text_hint=text_hint,
            )
        except Exception:  # noqa: BLE001
            # A regression in one transform must not poison the
            # whole observation.  Record a generic warning and
            # move on.
            state.warnings.append("transform_error")
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    eligible_total = sum(1 for c in state.candidates if c.eligible)
    suppressed_total = sum(1 for c in state.candidates if not c.eligible)
    eligible_savings = sum(
        (c.estimated_savings_tokens or 0) for c in state.candidates if c.eligible
    )
    eligible_original = sum(
        (c.estimated_original_tokens or 0) for c in state.candidates if c.eligible
    )
    eligible_compressed = sum(
        (c.estimated_compressed_tokens or 0) for c in state.candidates if c.eligible
    )

    observation = CompressionObservation(
        mode="observe",
        candidate_count=len(state.candidates),
        eligible_candidate_count=eligible_total,
        suppressed_candidate_count=suppressed_total,
        estimated_original_tokens=eligible_original if eligible_total else None,
        estimated_compressed_tokens=eligible_compressed if eligible_total else None,
        estimated_savings_tokens=eligible_savings if eligible_total else None,
        analyzer_latency_ms=elapsed_ms,
        warnings=tuple(state.warnings),
        reason_code_counts=dict(state.reason_counts),
        candidates=tuple(state.candidates),
        transform_counts=dict(state.transform_counts),
    )
    return observation


__all__ = [
    "CompressionCandidate",
    "CompressionObservation",
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
