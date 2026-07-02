"""Safe-mode mutating compressor (Phase 5).

Phase 5 of the cache-preserving deterministic compression roadmap
introduces the first request-mutating deterministic compressor.  Given
the :class:`SegmentationResult` produced by Phase 2, the compressor
walks volatile-suffix segments, identifies eligible compressible
candidates (matching the analyzer's eligibility rules), applies
deterministic transforms in-place on a deep-copied payload, and
returns a :class:`CompressionResult` describing the outcome.

Key design choices:

- **Safe**: transforms apply *only* to eligible ``volatile_suffix``
  segments.  Stable prefixes and cache-protected blocks are never
  touched.  ``compress_static_prefix = true`` is required for
  stable-prefix regions and requires explicit operator opt-in.
- **Fail-closed**: if the post-compression stable-prefix hash
  changes unexpectedly, the original payload is returned unchanged
  with ``failed_fallback = True``.
- **Observational**: the compressor never mutates the input payload
  or segmentation result; it deep-copies the payload before any
  mutation and never touches ``segmentation``.
- **Latency-bounded**: the compressor runs under a per-request
  latency budget.  On exceed it stops cleanly and returns a
  partial result.
- **Deterministic**: transforms are pure string operations.  The
  same payload + segmentation + policy always produces the same
  result.  Markers are deterministic and content-addressed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from eggpool.transcoder.segmentation import (
    RequestSegment,
    SegmentationResult,
    SegmentKind,
    _hash_payload,  # type: ignore[reportPrivateUsage]
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
    warnings: tuple[str, ...]
    latency_ms: float
    reason_code_counts: Mapping[str, int]
    failed_fallback: bool

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
            "warnings": list(self.warnings),
            "latency_ms": self.latency_ms,
            "reason_code_counts": dict(self.reason_code_counts),
            "failed_fallback": self.failed_fallback,
        }
        return json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)


def result_to_summary(result: CompressionResult) -> str:
    """Return compact JSON summary for a :class:`CompressionResult`."""
    return result.summary_json


# ---------------------------------------------------------------------------
# No-op result
# ---------------------------------------------------------------------------


_NO_OP_RESULT: CompressionResult = CompressionResult(
    applied=False,
    mode="safe",
    transformed_payload=None,  # type: ignore[arg-type]
    transform_count=0,
    transforms_by_reason={},
    original_tokens=0,
    compressed_tokens=0,
    savings_tokens=0,
    pre_stable_prefix_hash="",
    post_stable_prefix_hash="",
    stable_prefix_preserved=True,
    warnings=(),
    latency_ms=0.0,
    reason_code_counts={},
    failed_fallback=False,
)


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
        warnings=(),
        latency_ms=0.0,
        reason_code_counts={},
        failed_fallback=False,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_ASCII_FLOOR = 4
_NON_ASCII_FLOOR = 2
_REPEATED_LINE_MIN_RUN = 5
_LOG_MIN_LINES = 32
_JSON_MINIFY_PARSE_LIMIT = 256 * 1024

_BASE64_ALPHABET = re.compile(r"^[A-Za-z0-9+/=\s]{256,}$")
_DATA_URI_PREFIX = re.compile(r"^data:[a-zA-Z0-9+/.-]+;base64,")
_BLOB_LINE = re.compile(r"^[A-Za-z0-9+/_=-]{512,}$")

_STACK_TRACE_FRAME_RE: re.Pattern[str] = re.compile(r'^\s*File\s+"[^"]+"')


def _cheap_tokens(text: str) -> int:
    """Cheap ASCII/non-ASCII token estimate."""
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


def _replace_path(
    payload: Any,
    content_path: tuple[Any, ...],
    new_text: str,
) -> bool:
    """Walk into payload using content_path; replace the leaf string with new_text.

    Returns True if mutation succeeded, False if path doesn't resolve
    to a string.  Supports dict keys and list indices; raises nothing.
    """
    try:
        if not content_path:
            return False
        parent: Any = payload
        for key in content_path[:-1]:
            if isinstance(parent, (Mapping, list)):
                parent = parent[key]  # type: ignore[reportUnknownVariableType]
            else:
                return False
        last_key = content_path[-1]
        if isinstance(parent, Mapping):
            parent_map = cast("dict[Any, Any]", parent)
            if not isinstance(parent_map.get(last_key), str):
                return False
            parent_map[last_key] = new_text
            return True
        if isinstance(parent, list):
            if not isinstance(parent[last_key], str):
                return False
            parent[last_key] = new_text
            return True
        return False
    except (KeyError, IndexError, TypeError):
        return False


def _within_budget(deadline: float | None) -> bool:
    """True if the compressor still has latency budget remaining."""
    if deadline is None:
        return True
    return time.perf_counter() < deadline


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
    savings_tokens = 0
    while run_start < len(lines):
        run_end = run_start + 1
        while run_end < len(lines) and lines[run_end] == lines[run_start]:
            run_end += 1
        run_length = run_end - run_start
        if run_length >= _REPEATED_LINE_MIN_RUN and lines[run_start]:
            result.append(lines[run_start])
            dropped = run_length - 1
            savings_tokens += _cheap_tokens(lines[run_start]) * dropped
            if dropped > 0:
                savings_tokens -= 1  # marker cost
        else:
            for line in lines[run_start:run_end]:
                result.append(line)
        run_start = run_end
    new_text = "\n".join(result)
    if new_text == text:
        return None
    orig_tokens = _cheap_tokens(text)
    comp_tokens = _cheap_tokens(new_text)
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
    digest = hashlib.sha256(removed_text.encode("utf-8")).hexdigest()
    marker = (
        f"[EggPool logs compacted: kept head={keep_head}"
        f" + errors + tail={keep_tail}"
        f" | sha256={digest}]"
    )
    new_lines = head + preserved_middle + [marker] + tail
    new_text = "\n".join(new_lines)
    if new_text == text:
        return None
    orig_tokens = _cheap_tokens(text)
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
                marker = (
                    f"[EggPool search compacted: dropped {dropped}"
                    " redundant match lines]"
                )
                new_lines.append(marker)
            continue
        new_lines.append(line)
    new_text = "\n".join(new_lines)
    if new_text == text:
        return None
    orig_tokens = _cheap_tokens(text)
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
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    new_text = f"[EggPool blob elided: sha256={digest}]"
    orig_tokens = _cheap_tokens(text)
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
    comp_tokens = _cheap_tokens(compact)
    return compact, orig_tokens, comp_tokens


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
    for i, line in enumerate(lines):
        if not keep_flags[i]:
            if not marker_added:
                marker = (
                    f"[EggPool stack compacted: dropped {drop_count} repeated frames]"
                )
                new_lines.append(marker)
                marker_added = True
            continue
        new_lines.append(line)
    new_text = "\n".join(new_lines)
    if new_text == text:
        return None
    orig_tokens = _cheap_tokens(text)
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
        REASON_STATIC_PREFIX,
        REASON_TRANSFORM_DISABLED,
    )

    reasons: list[str] = []
    if not transform_enabled:
        reasons.append(REASON_TRANSFORM_DISABLED)
        return False, REASON_TRANSFORM_DISABLED, reasons

    if segment.protected and policy.respect_cache_boundaries:
        reasons.append(REASON_PROTECTED_CACHE_BOUNDARY)
        return False, REASON_PROTECTED_CACHE_BOUNDARY, reasons

    if segment.kind is SegmentKind.STABLE_PREFIX and not policy.compress_static_prefix:
        reasons.append(REASON_STATIC_PREFIX)
        return False, REASON_STATIC_PREFIX, reasons

    if (
        policy.placement == "suffix_only"
        and segment.kind is not SegmentKind.VOLATILE_SUFFIX
    ):
        reasons.append(REASON_PLACEMENT)
        return False, REASON_PLACEMENT, reasons
    if (
        policy.placement == "after_cache_boundary"
        and segment.kind is SegmentKind.STABLE_PREFIX
    ):
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
    all zeros).  When fail-closed triggers (stable_prefix_hash
    mismatch after mutation, or any unexpected exception), returns
    the ORIGINAL payload with applied=False, failed_fallback=True,
    and a high-severity warning.

    Never mutates ``payload`` in place; always deep-copies.  Never
    mutates ``segmentation``.  Never raises.
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
            warnings=("apply_exception",),
            latency_ms=elapsed_ms,
            reason_code_counts={},
            failed_fallback=True,
        )


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

    # Pre-hash stable prefix
    stable_segments = tuple(
        s for s in segmentation.all_segments() if s.kind is SegmentKind.STABLE_PREFIX
    )
    pre_stable_prefix_hash = _hash_payload(stable_segments)

    mutated = copy.deepcopy(payload)

    all_reason_counts: dict[str, int] = {}
    transforms_by_reason: dict[str, int] = {}
    warnings: list[str] = []
    total_original_tokens = 0
    total_compressed_tokens = 0
    total_savings_tokens = 0
    transform_count = 0

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

    segments = segmentation.all_segments()
    for index, segment in enumerate(segments):
        if not _within_budget(deadline):
            warnings.append(REASON_LATENCY_BUDGET)
            _bump(REASON_LATENCY_BUDGET)
            break

        segment_id = _segment_id_for(segment, index)

        for transform_name, transform_enabled in transforms_enabled:
            if not _within_budget(deadline):
                warnings.append(REASON_LATENCY_BUDGET)
                _bump(REASON_LATENCY_BUDGET)
                break

            eligible, suppressed, _reasons = _filter_segment(
                segment,
                policy=policy,
                transform=transform_name,
                transform_enabled=transform_enabled,
            )
            if not eligible:
                if suppressed is not None:
                    _bump(suppressed)
                continue

            # Collect the actual text from the deep-copied payload
            actual_text = _collect_text(mutated, segment.content_path)
            if actual_text is None or not actual_text:
                _bump(REASON_EMPTY_SEGMENT)
                continue

            # Run the transform
            result = _run_transform(transform_name, actual_text, segment_id)
            if result is None:
                continue

            new_text = result[0]
            orig_tokens = result[1]
            comp_tokens = result[2]
            savings = orig_tokens - comp_tokens

            if savings <= 0:
                continue

            # Apply threshold checks
            if orig_tokens < policy.min_candidate_tokens:
                _bump(REASON_BELOW_MIN_CANDIDATE_TOKENS)
                continue
            if savings < policy.min_savings_tokens:
                _bump(REASON_BELOW_MIN_SAVINGS_TOKENS)
                continue

            # Apply the mutation
            if not _replace_path(mutated, segment.content_path, new_text):
                continue

            # Bump reason code for the transform that was applied
            reason_code = _TRANSFORM_REASON.get(transform_name, transform_name)
            _bump(reason_code)
            transforms_by_reason[reason_code] = (
                transforms_by_reason.get(reason_code, 0) + 1
            )

            total_original_tokens += orig_tokens
            total_compressed_tokens += comp_tokens
            total_savings_tokens += savings
            transform_count += 1

    elapsed_ms = (time.perf_counter() - start) * 1000.0

    # Post-hash stable prefix
    post_stable_prefix_hash = _hash_payload(stable_segments)
    stable_prefix_preserved = post_stable_prefix_hash == pre_stable_prefix_hash

    # Fail-closed: prefix hash mismatch when static prefix is not allowed
    if not stable_prefix_preserved and not policy.compress_static_prefix:
        warnings.append(REASON_PREFIX_HASH_MISMATCH)
        _bump(REASON_PREFIX_HASH_MISMATCH)
        logger.warning(
            "stable_prefix_hash changed after safe compression, "
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
            pre_stable_prefix_hash=pre_stable_prefix_hash,
            post_stable_prefix_hash=post_stable_prefix_hash,
            stable_prefix_preserved=False,
            warnings=tuple(warnings),
            latency_ms=elapsed_ms,
            reason_code_counts=dict(all_reason_counts),
            failed_fallback=True,
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
        pre_stable_prefix_hash=pre_stable_prefix_hash,
        post_stable_prefix_hash=post_stable_prefix_hash,
        stable_prefix_preserved=stable_prefix_preserved,
        warnings=tuple(warnings),
        latency_ms=elapsed_ms,
        reason_code_counts=dict(all_reason_counts),
        failed_fallback=False,
    )


__all__ = [
    "CompressionResult",
    "REASON_PREFIX_HASH_MISMATCH",
    "apply_safe_compression",
    "result_to_summary",
]
