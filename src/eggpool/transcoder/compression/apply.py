"""Safe-mode mutating compressor (Phase 5).

Phase 5 of the cache-preserving deterministic compression roadmap
introduces the first request-mutating deterministic compressor.  Given
the :class:`SegmentationResult` produced by Phase 2, the compressor
walks volatile-suffix segments, identifies eligible compressible
candidates (matching the analyzer's eligibility rules), discovers
planned deterministic replacements, and applies them through
path-level copy-on-write *only* when at least one mutation is
needed.  The input payload is never mutated; no-op requests return
the original payload object unchanged; applied requests copy only the
dict/list ancestors on each mutated replacement path and share
untouched subtrees by reference (this is **not** a full deep copy).
A :class:`CompressionResult` describing the outcome is returned.

Key design choices:

- **Safe**: transforms apply *only* to eligible ``volatile_suffix``
  segments.  Stable prefixes and cache-protected blocks are never
  touched.
- **Fail-closed**: if the post-compression stable-prefix hash
  changes unexpectedly, the original payload is returned unchanged
  with ``failed_fallback = True``.
- **Observational**: the compressor never mutates the input payload
  or segmentation result.  It discovers planned replacements first,
  then applies them through path-level copy-on-write *only* when at
  least one mutation is needed.  No-op requests return the original
  payload object unchanged; applied requests copy only the mutated
  leaves and the dict/list ancestors on each replacement path,
  sharing unchanged subtrees by reference.  This is not a deep
  copy: it is a selective structural copy that preserves the
  ``input is never mutated`` invariant without paying for a full
  payload clone when no transform fires.  Segmentation is never
  touched.
- **Latency-bounded**: the compressor runs under a per-request
  latency budget.  On exceed it stops cleanly and returns a
  partial result.
- **Deterministic**: transforms are pure string operations.  The
  same payload + segmentation + policy always produces the same
  result.  Markers are deterministic and content-addressed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from eggpool.request.limits import estimate_text_tokens
from eggpool.transcoder.compression.markers import build_marker
from eggpool.transcoder.segmentation import (
    RequestSegment,
    SegmentationResult,
    SegmentKind,
    stable_prefix_content_hash,
)

if TYPE_CHECKING:
    from eggpool.transcoder.compression.policy import CompressionConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------------------

REASON_PREFIX_HASH_MISMATCH: str = "stable_prefix_hash_mismatch"

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompressionResult:
    """Outcome of a safe-mode compression run.

    The finalizer persists a compact summary; the dashboard API can
    return the full structure for drill-in.  Raw request content is
    never stored.

    Candidate counting invariants:

    * ``candidate_count`` counts every (segment, transform) pair the
      applier actually walked past the segment/text fetch guards.
    * ``eligible_candidate_count`` counts the subset that survived
      :func:`_filter_segment` policy filters.
    * ``suppressed_candidate_count`` counts the candidates rejected
      by ``_filter_segment`` (transform disabled, protected cache
      boundary, static prefix without opt-in, placement mismatch) or
      by per-segment guards (empty text, below ``min_candidate_tokens``,
      below ``min_savings_tokens``, latency budget).
    * ``applied_transform_count`` is the count of candidates that
      actually produced a planned replacement and survived fail-closed
      stable-prefix verification.  When ``applied is True`` this
      equals the number of planned replacements that landed.
    """

    applied: bool
    mode: str
    transformed_payload: Any
    transform_count: int
    transforms_by_reason: Mapping[str, int]
    original_tokens: int
    compressed_tokens: int
    savings_tokens: int
    pre_stable_prefix_hash: str
    post_stable_prefix_hash: str
    stable_prefix_preserved: bool
    stable_prefix_shape_hash: str
    stable_prefix_content_hash: str
    warnings: tuple[str, ...]
    latency_ms: float
    reason_code_counts: Mapping[str, int]
    failed_fallback: bool
    candidate_count: int = 0
    eligible_candidate_count: int = 0
    suppressed_candidate_count: int = 0
    applied_transform_count: int = 0

    @property
    def summary_json(self) -> str:
        """Compact JSON summary for persistence."""
        payload = {
            "applied": self.applied,
            "mode": self.mode,
            "transform_count": self.transform_count,
            "transforms_by_reason": dict(self.transforms_by_reason),
            "original_tokens": self.original_tokens,
            "compressed_tokens": self.compressed_tokens,
            "savings_tokens": self.savings_tokens,
            "pre_stable_prefix_hash": self.pre_stable_prefix_hash,
            "post_stable_prefix_hash": self.post_stable_prefix_hash,
            "stable_prefix_preserved": self.stable_prefix_preserved,
            "stable_prefix_shape_hash": self.stable_prefix_shape_hash,
            "stable_prefix_content_hash": self.stable_prefix_content_hash,
            "warnings": list(self.warnings),
            "latency_ms": self.latency_ms,
            "reason_code_counts": dict(self.reason_code_counts),
            "failed_fallback": self.failed_fallback,
            "candidate_count": self.candidate_count,
            "eligible_candidate_count": self.eligible_candidate_count,
            "suppressed_candidate_count": self.suppressed_candidate_count,
            "applied_transform_count": self.applied_transform_count,
        }
        return json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)


def result_to_summary(result: CompressionResult) -> str:
    """Return compact JSON summary for a :class:`CompressionResult`."""
    return result.summary_json


@dataclass(frozen=True, slots=True)
class SafeModeObservation:
    """Adapter that exposes a safe-applied run as a CompressionObservation.

    The dashboard / finalizer pair duck-types against the original
    ``CompressionObservation`` shape; we don't need to instantiate one
    because the finalizer only reads documented attributes.  This
    class is what :func:`build_safe_mode_observation` returns when the
    safe-mode applier is run as the single observation source.
    """

    mode: str
    candidate_count: int
    eligible_candidate_count: int
    suppressed_candidate_count: int
    estimated_original_tokens: int | None
    estimated_compressed_tokens: int | None
    estimated_savings_tokens: int | None
    analyzer_latency_ms: float
    warnings: tuple[str, ...]
    reason_code_counts: dict[str, int]
    transform_counts: dict[str, int]
    candidates: tuple[()]

    def to_summary_json(self) -> str:
        """Compact JSON summary for storage and dashboard drill-in."""
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
            "candidates": [],
            "source": "safe_apply",
        }
        return json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)

    def candidate_summary(self) -> dict[str, int]:
        """Return applier-derived candidate counts for dashboards / tests.

        Keys are stable and always present (zero when no candidates
        were considered).  Compare against the observe-mode
        :class:`CompressionObservation` for consistency.
        """
        return {
            "candidate_count": self.candidate_count,
            "eligible_candidate_count": self.eligible_candidate_count,
            "suppressed_candidate_count": self.suppressed_candidate_count,
        }


def build_safe_mode_observation(result: CompressionResult) -> SafeModeObservation:
    """Build a :class:`SafeModeObservation` from an applied :class:`CompressionResult`.

    The finalizer and dashboard already duck-type against the
    observe-mode ``CompressionObservation`` shape, so this adapter
    fills the same fields from a single safe-mode run.  Candidate,
    eligible, suppressed, and applied counts are taken directly from
    the applier-derived :class:`CompressionResult` fields populated
    during the single safe-mode pass; the observation therefore
    reflects the *real* suppression decisions (transform disabled,
    protected cache boundary,
    placement mismatch, empty segment, below min_candidate_tokens,
    below min_savings_tokens, latency budget, fail-closed) without
    paying for a second observe analyzer pass and without silently
    under-reporting opportunities.
    """
    if result.applied:
        estimated_original = result.original_tokens or None
        estimated_compressed = result.compressed_tokens or None
        estimated_savings = result.savings_tokens or None
    else:
        # On no-op or fail-closed paths the applier does not have
        # settled token totals; surface them only when transforms ran.
        estimated_original = None
        estimated_compressed = None
        estimated_savings = None

    return SafeModeObservation(
        mode="safe",
        candidate_count=result.candidate_count,
        eligible_candidate_count=result.eligible_candidate_count,
        suppressed_candidate_count=result.suppressed_candidate_count,
        estimated_original_tokens=estimated_original,
        estimated_compressed_tokens=estimated_compressed,
        estimated_savings_tokens=estimated_savings,
        analyzer_latency_ms=result.latency_ms,
        warnings=result.warnings,
        reason_code_counts=dict(result.reason_code_counts),
        transform_counts=dict(result.transforms_by_reason),
        candidates=(),
    )


__all__ = [  # noqa: F822  (extended below)
    "CompressionResult",
    "REASON_PREFIX_HASH_MISMATCH",
    "SafeModeObservation",
    "apply_safe_compression",
    "build_safe_mode_observation",
    "result_to_summary",
]


def _noop_result(payload: Any) -> CompressionResult:
    """Return a no-op result with the original payload."""
    return CompressionResult(
        applied=False,
        mode="safe",
        transformed_payload=payload,
        transform_count=0,
        transforms_by_reason={},
        original_tokens=0,
        compressed_tokens=0,
        savings_tokens=0,
        pre_stable_prefix_hash="",
        post_stable_prefix_hash="",
        stable_prefix_preserved=True,
        stable_prefix_shape_hash="",
        stable_prefix_content_hash="",
        warnings=(),
        latency_ms=0.0,
        reason_code_counts={},
        failed_fallback=False,
        candidate_count=0,
        eligible_candidate_count=0,
        suppressed_candidate_count=0,
        applied_transform_count=0,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_REPEATED_LINE_MIN_RUN = 5
_LOG_MIN_LINES = 32
_JSON_MINIFY_PARSE_LIMIT = 256 * 1024

_BASE64_ALPHABET = re.compile(r"^[A-Za-z0-9+/=\s]{256,}$")
_DATA_URI_PREFIX = re.compile(r"^data:[a-zA-Z0-9+/.-]+;base64,")
_BLOB_LINE = re.compile(r"^[A-Za-z0-9+/_=-]{512,}$")

_STACK_TRACE_FRAME_RE: re.Pattern[str] = re.compile(r'^\s*File\s+"[^"]+"')


def _cheap_tokens(text: str) -> int:
    """Use the shared cheap estimator, including its ASCII fast path."""
    return estimate_text_tokens(text)


def _segment_id_for(segment: RequestSegment, index: int) -> str:
    """Replicate analyzer._segment_id exactly."""
    path = ".".join(str(p) for p in segment.content_path) or f"seg{index}"
    return f"s{index}:{segment.kind.value}:{path}"


def _collect_text(payload: Any, content_path: tuple[Any, ...]) -> str | None:
    """Walk into payload using content_path; return the leaf string or None."""
    try:
        current: Any = payload
        for key in content_path:
            if isinstance(current, (Mapping, list)):
                current = current[key]  # type: ignore[reportUnknownVariableType]
            else:
                return None
        if isinstance(current, str):
            return current
        return None
    except (KeyError, IndexError, TypeError):
        return None


def _within_budget(deadline: float | None) -> bool:
    """True if the compressor still has latency budget remaining."""
    if deadline is None:
        return True
    return time.perf_counter() < deadline


def _digest(text: str) -> str:
    """SHA-256 hex digest of UTF-8 text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Transform implementations
# ---------------------------------------------------------------------------


def _transform_fold_repeated_lines(
    text: str,
    segment_id: str,
) -> tuple[str, int, int] | None:
    """Collapse runs of identical adjacent lines of length >= 5.

    Returns ``(new_text, original_tokens, compressed_tokens)`` or
    ``None`` if no transform occurred.
    """
    if "\n" not in text:
        return None
    lines = text.split("\n")
    if len(lines) < _REPEATED_LINE_MIN_RUN:
        return None
    result: list[str] = []
    run_start = 0
    while run_start < len(lines):
        run_end = run_start + 1
        while run_end < len(lines) and lines[run_end] == lines[run_start]:
            run_end += 1
        run_length = run_end - run_start
        if run_length >= _REPEATED_LINE_MIN_RUN and lines[run_start]:
            result.append(lines[run_start])
        else:
            for line in lines[run_start:run_end]:
                result.append(line)
        run_start = run_end
    folded_text = "\n".join(result)
    if folded_text == text:
        return None
    orig_tokens = _cheap_tokens(text)
    orig_lines = text.count("\n") + 1
    marker = build_marker(
        "fold_repeated_lines",
        segment_id,
        orig_lines,
        orig_tokens,
        _digest(text),
    )
    new_text = folded_text + "\n" + marker
    comp_tokens = _cheap_tokens(new_text)
    if comp_tokens >= orig_tokens:
        return None
    return new_text, orig_tokens, comp_tokens


def _transform_compact_logs(
    text: str,
    segment_id: str,
) -> tuple[str, int, int, int] | None:
    """Compact large log/command-output blocks.

    Returns ``(new_text, original_tokens, compressed_tokens,
    removed_lines)`` or None.
    """
    if "\n" not in text:
        return None
    lines = text.split("\n")
    if len(lines) < _LOG_MIN_LINES:
        return None
    keep_head = 8
    keep_tail = 8
    if len(lines) <= keep_head + keep_tail:
        return None
    head = lines[:keep_head]
    tail = lines[-keep_tail:]
    middle = lines[keep_head:-keep_tail]
    # Keep error/diagnostic lines from middle
    preserved_middle: list[str] = []
    for line in middle:
        upper = line.upper()
        if (
            "ERROR" in upper
            or "FATAL" in upper
            or "EXCEPTION" in upper
            or "PANIC" in upper
            or "FAILED" in upper
        ):
            preserved_middle.append(line)
    removed_count = len(middle) - len(preserved_middle)
    if removed_count <= 0:
        return None
    # Build marker with digest of removed content
    removed_text = "\n".join(middle)
    digest = _digest(removed_text)
    orig_tokens = _cheap_tokens(text)
    marker = build_marker(
        "compact_logs",
        segment_id,
        len(lines),
        orig_tokens,
        digest,
    )
    new_lines = head + preserved_middle + [marker] + tail
    new_text = "\n".join(new_lines)
    if new_text == text:
        return None
    comp_tokens = _cheap_tokens(new_text)
    return new_text, orig_tokens, comp_tokens, removed_count


def _transform_compact_search_results(
    text: str,
    segment_id: str,
) -> tuple[str, int, int, int] | None:
    """Compact search/diff result blocks.

    Returns ``(new_text, original_tokens, compressed_tokens,
    dropped_lines)`` or None.
    """
    lines = text.split("\n")
    if len(lines) < 16:
        return None
    # Require at least some structural markers (diff, @@, file paths)
    # to avoid firing on plain text.
    has_structural = any(
        line.startswith(("diff ", "@@ ", "---", "+++", "Binary "))
        or (":" in line and (line.startswith("/") or line.startswith("./")))
        for line in lines
    )
    if not has_structural:
        return None
    # Mark lines to keep
    keep_flags = [False] * len(lines)
    for i, line in enumerate(lines):
        if line.startswith(("diff ", "@@ ", "---", "+++", "Binary ")) or (
            ":" in line and (line.startswith("/") or line.startswith("./"))
        ):
            keep_flags[i] = True
    # Drop middle 50% of non-kept lines
    start = len(lines) // 4
    end = 3 * len(lines) // 4
    dropped = 0
    for i in range(start, end):
        if not keep_flags[i] and lines[i]:
            keep_flags[i] = True  # temporary: mark to drop
            dropped += 1
    if dropped <= 0:
        return None
    # Rebuild: keep non-marked lines
    new_lines: list[str] = []
    drop_count = 0
    orig_tokens = _cheap_tokens(text)
    digest = _digest(text)
    for i, line in enumerate(lines):
        if (
            start <= i < end
            and line
            and not any(
                line.startswith(p) for p in ("diff ", "@@ ", "---", "+++", "Binary ")
            )
            and not (":" in line and (line.startswith("/") or line.startswith("./")))
        ):
            drop_count += 1
            if drop_count == 1:
                marker = build_marker(
                    "compact_search_results",
                    segment_id,
                    len(lines),
                    orig_tokens,
                    digest,
                )
                new_lines.append(marker)
            continue
        new_lines.append(line)
    new_text = "\n".join(new_lines)
    if new_text == text:
        return None
    comp_tokens = _cheap_tokens(new_text)
    return new_text, orig_tokens, comp_tokens, dropped


def _transform_elide_base64_blobs(
    text: str,
    segment_id: str,
) -> tuple[str, int, int] | None:
    """Elide opaque base64 / data-URI / long single-line blobs.

    Returns ``(new_text, original_tokens, compressed_tokens)`` or None.
    """
    stripped = text.strip()
    if not stripped:
        return None
    is_blob = (
        bool(_DATA_URI_PREFIX.search(stripped))
        or bool(_BLOB_LINE.match(stripped.splitlines()[0]))
        or bool(_BASE64_ALPHABET.match(stripped))
    )
    if not is_blob:
        return None
    digest = _digest(text)
    orig_tokens = _cheap_tokens(text)
    original_lines = text.count("\n") + 1
    new_text = build_marker(
        "elide_base64_blobs",
        segment_id,
        original_lines,
        orig_tokens,
        digest,
    )
    comp_tokens = _cheap_tokens(new_text)
    return new_text, orig_tokens, comp_tokens


def _transform_minify_machine_json(
    text: str,
    segment_id: str,
) -> tuple[str, int, int] | None:
    """Minify whitespace in machine-generated JSON.

    Returns ``(new_text, original_tokens, compressed_tokens)`` or None.
    """
    stripped = text.lstrip()
    if not stripped or stripped[0] not in ("{", "["):
        return None
    if len(text) > _JSON_MINIFY_PARSE_LIMIT:
        return None
    try:
        parsed: Any = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, (Mapping, list)):
        return None
    compact = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
    if len(compact) >= len(text):
        return None
    orig_tokens = _cheap_tokens(text)
    original_lines = text.count("\n") + 1
    marker = build_marker(
        "minify_machine_json",
        segment_id,
        original_lines,
        orig_tokens,
        _digest(text),
    )
    new_text = compact + "\n" + marker
    comp_tokens = _cheap_tokens(new_text)
    if comp_tokens >= orig_tokens:
        return None
    return new_text, orig_tokens, comp_tokens


def _transform_compact_stack_traces(
    text: str,
    segment_id: str,
) -> tuple[str, int, int, int] | None:
    """Collapse repeated stack frames.

    Returns ``(new_text, original_tokens, compressed_tokens,
    dropped_frames)`` or None.
    """
    lines = text.split("\n")
    if len(lines) < 12:
        return None
    frame_indices = [
        i for i, line in enumerate(lines) if 'File "' in line and ", line " in line
    ]
    if len(frame_indices) < 4:
        return None
    seen: set[str] = set()
    drop_count = 0
    keep_flags = [True] * len(lines)
    for idx in frame_indices:
        line = lines[idx]
        if line in seen:
            keep_flags[idx] = False
            drop_count += 1
        else:
            seen.add(line)
    if drop_count <= 0:
        return None
    new_lines: list[str] = []
    marker_added = False
    orig_tokens = _cheap_tokens(text)
    digest = _digest(text)
    for i, line in enumerate(lines):
        if not keep_flags[i]:
            if not marker_added:
                marker = build_marker(
                    "compact_stack_traces",
                    segment_id,
                    len(lines),
                    orig_tokens,
                    digest,
                )
                new_lines.append(marker)
                marker_added = True
            continue
        new_lines.append(line)
    new_text = "\n".join(new_lines)
    if new_text == text:
        return None
    comp_tokens = _cheap_tokens(new_text)
    return new_text, orig_tokens, comp_tokens, drop_count


# ---------------------------------------------------------------------------
# Transform dispatcher
# ---------------------------------------------------------------------------


def _run_transform(
    transform: str,
    text: str,
    segment_id: str,
) -> tuple[str, int, int] | tuple[str, int, int, int] | None:
    """Dispatch to the matching transform.  Never raises."""
    try:
        if transform == "fold_repeated_lines":
            return _transform_fold_repeated_lines(text, segment_id)
        if transform == "compact_logs":
            result = _transform_compact_logs(text, segment_id)
            if result is not None:
                return result[0], result[1], result[2]
            return None
        if transform == "compact_search_results":
            result = _transform_compact_search_results(text, segment_id)
            if result is not None:
                return result[0], result[1], result[2]
            return None
        if transform == "elide_base64_blobs":
            return _transform_elide_base64_blobs(text, segment_id)
        if transform == "minify_machine_json":
            return _transform_minify_machine_json(text, segment_id)
        if transform == "compact_stack_traces":
            result = _transform_compact_stack_traces(text, segment_id)
            if result is not None:
                return result[0], result[1], result[2]
            return None
    except Exception:  # noqa: BLE001
        return None
    return None


# Reason code mapping for transform name -> reason code
_TRANSFORM_REASON: dict[str, str] = {
    "fold_repeated_lines": "repeated_line_run",
    "compact_logs": "log_compaction",
    "compact_search_results": "search_compaction",
    "elide_base64_blobs": "base64_elision",
    "minify_machine_json": "json_minify",
    "compact_stack_traces": "stack_trace_compaction",
}


# ---------------------------------------------------------------------------
# Per-segment policy filtering (mirrors analyzer._filter_segment)
# ---------------------------------------------------------------------------


def _filter_segment(
    segment: RequestSegment,
    *,
    policy: CompressionConfig,
    transform: str,
    transform_enabled: bool,
) -> tuple[bool, str | None, list[str]]:
    """Apply policy filtering to a candidate segment.

    Returns ``(eligible, suppressed_reason, reasons)``.
    """
    from eggpool.transcoder.compression.analyzer import (
        REASON_PLACEMENT,
        REASON_PROTECTED_CACHE_BOUNDARY,
        REASON_TRANSFORM_DISABLED,
    )

    reasons: list[str] = []
    if not transform_enabled:
        reasons.append(REASON_TRANSFORM_DISABLED)
        return False, REASON_TRANSFORM_DISABLED, reasons

    if segment.protected and policy.respect_cache_boundaries:
        reasons.append(REASON_PROTECTED_CACHE_BOUNDARY)
        return False, REASON_PROTECTED_CACHE_BOUNDARY, reasons

    if segment.kind is not SegmentKind.VOLATILE_SUFFIX:
        reasons.append(REASON_PLACEMENT)
        return False, REASON_PLACEMENT, reasons

    return True, None, reasons


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def apply_safe_compression(
    payload: Any,
    segmentation: SegmentationResult,
    *,
    policy: CompressionConfig,
    text_hints: Mapping[str, str] | None = None,
) -> CompressionResult:
    """Apply safe-mode deterministic compression to volatile-suffix segments.

    Returns a :class:`CompressionResult` describing the mutation.
    When ``policy.mode != "safe"`` or ``policy.enabled is False``,
    returns a no-op result (applied=False, transformed_payload=payload,
    all zeros).  When fail-closed triggers — the
    ``stable_prefix_content_hash`` differs between the original and
    mutated payload, or any
    unexpected exception is raised — returns the ORIGINAL payload with
    applied=False, failed_fallback=True, and a high-severity warning.

    The fail-closed check uses the exact content hash, not the
    structural shape hash.  The shape hash is content-private and
    useful for dashboard grouping, but it is not sufficient to detect
    an accidental stable-prefix mutation; only the content hash can.

    Never mutates the input ``payload`` or ``segmentation``.  When
    no replacement is needed the *same* payload object is returned
    by identity; when a replacement fires a path-level
    copy-on-write payload is returned that copies only the dict/list
    ancestors on each mutated path and leaves unchanged subtrees
    shared by reference.  This is **not** a deep copy: it is a
    selective structural copy that preserves the ``input is never
    mutated`` invariant while avoiding the cost of cloning the full
    payload on no-op runs.  Never raises.
    """
    if (
        not policy.enabled or policy.mode != "safe" or segmentation is None  # type: ignore[reportUnnecessaryComparison]
    ):
        return _noop_result(payload)

    start = time.perf_counter()
    deadline = start + (policy.max_compression_latency_ms / 1000.0)

    try:
        return _apply_safe_compression_impl(
            payload,
            segmentation,
            policy=policy,
            text_hints=text_hints,
            start=start,
            deadline=deadline,
        )
    except Exception:  # noqa: BLE001
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        logger.warning(
            "apply_safe_compression failed, returning original",
            exc_info=True,
        )
        return CompressionResult(
            applied=False,
            mode="safe",
            transformed_payload=payload,
            transform_count=0,
            transforms_by_reason={},
            original_tokens=0,
            compressed_tokens=0,
            savings_tokens=0,
            pre_stable_prefix_hash="",
            post_stable_prefix_hash="",
            stable_prefix_preserved=True,
            stable_prefix_shape_hash="",
            stable_prefix_content_hash="",
            warnings=("apply_exception",),
            latency_ms=elapsed_ms,
            reason_code_counts={},
            failed_fallback=True,
            candidate_count=0,
            eligible_candidate_count=0,
            suppressed_candidate_count=0,
            applied_transform_count=0,
        )


@dataclass(frozen=True, slots=True)
class _PlannedReplacement:
    """A single planned mutation from the discovery stage."""

    content_path: tuple[Any, ...]
    new_text: str
    orig_tokens: int
    comp_tokens: int
    reason_code: str
    segment_id: str


def _copy_with_replacements(
    payload: Any,
    replacements: list[_PlannedReplacement],
) -> Any:
    """Path-level copy-on-write: copy only dicts/lists on paths to mutated leaves.

    Unchanged subtrees are preserved by reference.  Multiple replacements
    sharing path prefixes copy each prefix dict/list exactly once.
    """
    if not replacements:
        return payload

    path_to_replacement: dict[tuple[Any, ...], _PlannedReplacement] = {}
    for repl in replacements:
        path_to_replacement[repl.content_path] = repl

    sorted_paths = sorted(path_to_replacement.keys(), key=len)
    copied: dict[int, Any] = {}

    def _shallow_copy(node: Any) -> Any:
        node_id = id(node)
        if node_id in copied:
            return copied[node_id]
        if isinstance(node, Mapping):
            result_node: Any = dict(node)  # type: ignore[assignment]
        elif isinstance(node, list):
            result_node = list(node)  # type: ignore[assignment]
        else:
            return node
        copied[node_id] = result_node
        return result_node

    result: Any = payload
    for path in sorted_paths:
        if not path:
            continue
        current: Any = result
        ancestors: list[Any] = []
        for key in path[:-1]:
            ancestors.append(current)
            if isinstance(current, (Mapping, list)):
                current = _shallow_copy(current[key])

        last_key = path[-1]
        if isinstance(current, (Mapping, list)):
            mutable_current: dict[str, Any] = current  # type: ignore[assignment]
            mutable_current[last_key] = path_to_replacement[path].new_text

        for i in range(len(ancestors) - 1, -1, -1):
            parent = _shallow_copy(ancestors[i])
            if isinstance(parent, (Mapping, list)):
                mutable_parent: dict[str, Any] = parent  # type: ignore[assignment]
                mutable_parent[path[i]] = current
            current = parent  # pyright: ignore[reportUnknownVariableType]

        if not ancestors:
            current = _shallow_copy(payload)
            if isinstance(current, (Mapping, list)):
                mutable_current2: dict[str, Any] = current  # type: ignore[assignment]
                mutable_current2[last_key] = path_to_replacement[path].new_text
            result = current  # pyright: ignore[reportUnknownVariableType]
        else:
            result = current  # pyright: ignore[reportUnknownVariableType]

    final_result: Any = result
    return final_result


def _apply_safe_compression_impl(
    payload: Any,
    segmentation: SegmentationResult,
    *,
    policy: CompressionConfig,
    text_hints: Mapping[str, str] | None,
    start: float,
    deadline: float,
) -> CompressionResult:
    """Core implementation of :func:`apply_safe_compression`."""
    from eggpool.transcoder.compression.analyzer import (
        REASON_BELOW_MIN_CANDIDATE_TOKENS,
        REASON_BELOW_MIN_SAVINGS_TOKENS,
        REASON_EMPTY_SEGMENT,
        REASON_LATENCY_BUDGET,
    )

    pre_content_hash = stable_prefix_content_hash(
        cast("Mapping[str, Any]", payload),
        segmentation,
    )
    pre_shape_hash = segmentation.stable_prefix_hash

    all_reason_counts: dict[str, int] = {}
    transforms_by_reason: dict[str, int] = {}
    warnings: list[str] = []
    total_original_tokens = 0
    total_compressed_tokens = 0
    total_savings_tokens = 0
    transform_count = 0
    candidate_count = 0
    eligible_candidate_count = 0
    suppressed_candidate_count = 0
    applied_transform_count = 0

    transforms_enabled: list[tuple[str, bool]] = [
        ("fold_repeated_lines", policy.transforms.fold_repeated_lines),
        ("compact_logs", policy.transforms.compact_logs),
        ("compact_search_results", policy.transforms.compact_search_results),
        ("elide_base64_blobs", policy.transforms.elide_base64_blobs),
        ("minify_machine_json", policy.transforms.minify_machine_json),
        ("compact_stack_traces", policy.transforms.compact_stack_traces),
    ]

    def _bump(code: str) -> None:
        all_reason_counts[code] = all_reason_counts.get(code, 0) + 1

    planned: list[_PlannedReplacement] = []

    segments = segmentation.all_segments()
    for index, segment in enumerate(segments):
        if not _within_budget(deadline):
            warnings.append(REASON_LATENCY_BUDGET)
            _bump(REASON_LATENCY_BUDGET)
            break

        segment_id = _segment_id_for(segment, index)
        current_text: str | None = _collect_text(payload, segment.content_path)

        for transform_name, transform_enabled in transforms_enabled:
            if not _within_budget(deadline):
                warnings.append(REASON_LATENCY_BUDGET)
                _bump(REASON_LATENCY_BUDGET)
                break

            # Each (segment, transform) pair is exactly one candidate so
            # the applier-derived counts stay meaningful without a second
            # observe pass.
            candidate_count += 1

            eligible, suppressed, _reasons = _filter_segment(
                segment,
                policy=policy,
                transform=transform_name,
                transform_enabled=transform_enabled,
            )
            if not eligible:
                suppressed_candidate_count += 1
                if suppressed is not None:
                    _bump(suppressed)
                continue

            eligible_candidate_count += 1

            if current_text is None or not current_text:
                suppressed_candidate_count += 1
                _bump(REASON_EMPTY_SEGMENT)
                continue

            result = _run_transform(transform_name, current_text, segment_id)
            if result is None:
                # No-op transform execution (e.g. pattern did not match)
                # is recorded as a suppressed candidate so dashboard
                # metrics do not silently under-report.
                suppressed_candidate_count += 1
                continue

            new_text = result[0]
            orig_tokens = result[1]
            comp_tokens = result[2]
            savings = orig_tokens - comp_tokens

            if savings <= 0:
                suppressed_candidate_count += 1
                continue

            if orig_tokens < policy.min_candidate_tokens:
                suppressed_candidate_count += 1
                _bump(REASON_BELOW_MIN_CANDIDATE_TOKENS)
                continue
            if savings < policy.min_savings_tokens:
                suppressed_candidate_count += 1
                _bump(REASON_BELOW_MIN_SAVINGS_TOKENS)
                continue

            reason_code = _TRANSFORM_REASON.get(transform_name, transform_name)
            _bump(reason_code)
            transforms_by_reason[reason_code] = (
                transforms_by_reason.get(reason_code, 0) + 1
            )

            total_original_tokens += orig_tokens
            total_compressed_tokens += comp_tokens
            total_savings_tokens += savings
            transform_count += 1
            applied_transform_count += 1

            planned.append(
                _PlannedReplacement(
                    content_path=segment.content_path,
                    new_text=new_text,
                    orig_tokens=orig_tokens,
                    comp_tokens=comp_tokens,
                    reason_code=reason_code,
                    segment_id=segment_id,
                )
            )
            current_text = new_text

    elapsed_ms = (time.perf_counter() - start) * 1000.0

    if not planned:
        return CompressionResult(
            applied=False,
            mode="safe",
            transformed_payload=payload,
            transform_count=0,
            transforms_by_reason={},
            original_tokens=0,
            compressed_tokens=0,
            savings_tokens=0,
            pre_stable_prefix_hash=pre_content_hash,
            post_stable_prefix_hash=pre_content_hash,
            stable_prefix_preserved=True,
            stable_prefix_shape_hash=pre_shape_hash,
            stable_prefix_content_hash=pre_content_hash,
            warnings=tuple(warnings),
            latency_ms=elapsed_ms,
            reason_code_counts=dict(all_reason_counts),
            failed_fallback=False,
            candidate_count=candidate_count,
            eligible_candidate_count=eligible_candidate_count,
            suppressed_candidate_count=suppressed_candidate_count,
            applied_transform_count=0,
        )

    mutated = _copy_with_replacements(payload, planned)

    post_content_hash = stable_prefix_content_hash(
        cast("Mapping[str, Any]", mutated),
        segmentation,
    )
    stable_prefix_content_preserved = post_content_hash == pre_content_hash

    if not stable_prefix_content_preserved:
        warnings.append(REASON_PREFIX_HASH_MISMATCH)
        _bump(REASON_PREFIX_HASH_MISMATCH)
        logger.warning(
            "stable_prefix_content_hash changed after safe compression, "
            "returning original payload (fail-closed)"
        )
        return CompressionResult(
            applied=False,
            mode="safe",
            transformed_payload=payload,
            transform_count=0,
            transforms_by_reason={},
            original_tokens=0,
            compressed_tokens=0,
            savings_tokens=0,
            pre_stable_prefix_hash=pre_content_hash,
            post_stable_prefix_hash=post_content_hash,
            stable_prefix_preserved=False,
            stable_prefix_shape_hash=pre_shape_hash,
            stable_prefix_content_hash=post_content_hash,
            warnings=tuple(warnings),
            latency_ms=elapsed_ms,
            reason_code_counts=dict(all_reason_counts),
            failed_fallback=True,
            candidate_count=candidate_count,
            eligible_candidate_count=eligible_candidate_count,
            suppressed_candidate_count=suppressed_candidate_count,
            applied_transform_count=0,
        )

    return CompressionResult(
        applied=transform_count > 0,
        mode="safe",
        transformed_payload=mutated,
        transform_count=transform_count,
        transforms_by_reason=dict(transforms_by_reason),
        original_tokens=total_original_tokens,
        compressed_tokens=total_compressed_tokens,
        savings_tokens=total_savings_tokens,
        pre_stable_prefix_hash=pre_content_hash,
        post_stable_prefix_hash=post_content_hash,
        stable_prefix_preserved=stable_prefix_content_preserved,
        stable_prefix_shape_hash=pre_shape_hash,
        stable_prefix_content_hash=post_content_hash,
        warnings=tuple(warnings),
        latency_ms=elapsed_ms,
        reason_code_counts=dict(all_reason_counts),
        failed_fallback=False,
        candidate_count=candidate_count,
        eligible_candidate_count=eligible_candidate_count,
        suppressed_candidate_count=suppressed_candidate_count,
        applied_transform_count=applied_transform_count,
    )


__all__ = [
    "CompressionResult",
    "REASON_PREFIX_HASH_MISMATCH",
    "apply_safe_compression",
    "result_to_summary",
]
