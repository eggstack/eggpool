"""Canonical request segmentation for cache-preserving compression (Phase 2).

Phase 2 of the cache-preserving deterministic compression plan introduces a
structural segmentation layer that annotates canonical requests into
cache/compression regions without mutating the request.  The segmentation
gives later phases a safe way to preserve provider-cacheable stable
prefixes while identifying volatile suffixes that can be deterministically
compressed.

Key design choices:

- :class:`SegmentationResult` is a frozen, value-typed summary that
  callers can safely share across the coordinator, finalizer, and stats
  pipeline without defensive copies.  It never carries raw request
  content — only segment metadata, byte/token estimates, and hashes.
- Three segment kinds partition every request: ``stable_prefix``,
  ``semi_stable_context``, and ``volatile_suffix``.  Segmentation is
  conservative: when classification is uncertain the request defaults
  to ``semi_stable_context`` (preserving content rather than marking it
  as compressible).
- Token estimates are cheap.  The estimator reuses
  :func:`eggpool.request.limits._estimate_string_tokens` semantics
  (4 ASCII chars/token, 2 non-ASCII bytes/token) and never raises on
  malformed input.  Missing estimates remain ``None`` and never block
  request handling.
- The stable-prefix hash and request-shape hash are SHA-256 digests of
  the canonical structural representation.  Neither hash exposes prompt
  content directly — the stable-prefix hash is computed over the
  structural descriptor, not the raw text.  Both exclude request
  timestamp, request ID, selected account, and other unstable metadata.
- A :data:`SegmentationStatus` enum records whether segmentation
  succeeded, was a no-op, or encountered a parse failure.  This is
  persisted alongside the hashes so the dashboard can distinguish
  "request was not segmented" from "segmentation produced no segments".

This module is observational only.  It does not change request bodies,
route scoring, or eligibility; it produces metadata that downstream
phases (transcoder cache stability, observe-mode compression
accounting) will read.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public segment model
# ---------------------------------------------------------------------------


class SegmentKind(StrEnum):
    """Coarse classification of a request region.

    * ``stable_prefix`` — provider-cacheable material that should be
      preserved verbatim (system/developer instructions, tool schemas,
      persistent project rules, provider-native cache blocks).
    * ``semi_stable_context`` — rolling conversation state that may
      evolve over time but is not safe to compress by default (prior
      turns, selected file snippets, repo summaries).
    * ``volatile_suffix`` — recent, large, or log-like content that is
      a candidate for deterministic compression (latest user turn,
      tool outputs, command logs, search results, generated blobs).
    """

    STABLE_PREFIX = "stable_prefix"
    SEMI_STABLE_CONTEXT = "semi_stable_context"
    VOLATILE_SUFFIX = "volatile_suffix"


class SegmentSource(StrEnum):
    """Origin of a segment within the request body.

    Sources are protocol-agnostic; the segmentation pass maps
    protocol-specific roles/content types to the closest source.
    Unrecognized roles/content fall back to :attr:`UNKNOWN` so the
    module is forward-compatible with future wire-format additions.
    """

    SYSTEM = "system"
    DEVELOPER = "developer"
    TOOL_SCHEMA = "tool_schema"
    CACHE_CONTROL = "cache_control"
    PRIOR_MESSAGE = "prior_message"
    LATEST_USER_MESSAGE = "latest_user_message"
    TOOL_RESULT = "tool_result"
    COMMAND_OUTPUT = "command_output"
    SEARCH_RESULT = "search_result"
    BLOB = "blob"
    UNKNOWN = "unknown"


class SegmentationStatus(StrEnum):
    """Outcome of a segmentation pass.

    * ``segmented`` — segments were produced (the typical case).
    * ``empty_request`` — the request had no content to segment (only
      metadata fields like ``model``/``stream``).
    * ``parse_failure`` — the payload was not a dict or could not be
      classified; the result is a single ``unknown`` segment.
    """

    SEGMENTED = "segmented"
    EMPTY_REQUEST = "empty_request"
    PARSE_FAILURE = "parse_failure"


@dataclass(frozen=True, slots=True)
class RequestSegment:
    """A single annotated region of a request.

    ``message_index`` is the position in the request's message array
    (or ``None`` for non-message regions like tool schemas).  The
    ``content_path`` tuple identifies the sub-field for non-message
    regions (``("tools", 3)`` means the fourth tool in the tools list);
    for message regions it carries the role/content-block index.

    ``protected`` marks content that must never be marked as
    compressible: provider-cacheable prefixes, system/developer
    instructions, tool schemas, and provider-native cache_control
    blocks.  ``compressible_candidate`` is True for ``volatile_suffix``
    regions that may be safely compressed in later phases — the
    segmentation pass never compresses anything itself, it only labels
    candidates.
    """

    kind: SegmentKind
    source: SegmentSource
    message_index: int | None
    content_path: tuple[Any, ...]
    byte_length: int
    estimated_tokens: int | None
    protected: bool
    compressible_candidate: bool
    reason: str


@dataclass(frozen=True, slots=True)
class SegmentationResult:
    """Frozen summary of a segmentation pass.

    The :class:`RequestSegment` list, segment-kind counts, byte/token
    estimates, and deterministic hashes are the public surface.  Raw
    request content never enters this object — only the structural
    descriptor used to compute the hashes.
    """

    status: SegmentationStatus
    segments: tuple[RequestSegment, ...]
    segment_count_by_kind: Mapping[SegmentKind, int]
    stable_prefix_bytes: int
    semi_stable_bytes: int
    volatile_bytes: int
    stable_prefix_estimated_tokens: int | None
    semi_stable_estimated_tokens: int | None
    volatile_estimated_tokens: int | None
    stable_prefix_hash: str
    request_shape_hash: str
    cache_control_present: bool

    def compressible_candidate_count(self) -> int:
        """Number of segments marked as future compression candidates."""
        return sum(1 for s in self.segments if s.compressible_candidate)

    def protected_count(self) -> int:
        """Number of segments marked as protected (never compressible)."""
        return sum(1 for s in self.segments if s.protected)

    def count_by_kind(self) -> Mapping[SegmentKind, int]:
        """Count of segments per kind; same object as ``segment_count_by_kind``."""
        return self.segment_count_by_kind

    @property
    def stable_prefix_segments(self) -> tuple[RequestSegment, ...]:
        """All segments in the ``stable_prefix`` kind."""
        return tuple(s for s in self.segments if s.kind is SegmentKind.STABLE_PREFIX)

    @property
    def semi_stable_segments(self) -> tuple[RequestSegment, ...]:
        """All segments in the ``semi_stable_context`` kind."""
        return tuple(
            s for s in self.segments if s.kind is SegmentKind.SEMI_STABLE_CONTEXT
        )

    @property
    def volatile_segments(self) -> tuple[RequestSegment, ...]:
        """All segments in the ``volatile_suffix`` kind."""
        return tuple(s for s in self.segments if s.kind is SegmentKind.VOLATILE_SUFFIX)

    @property
    def total_estimated_tokens(self) -> int | None:
        """Sum of segment-level token estimates, or ``None`` if any are missing."""
        if (
            self.stable_prefix_estimated_tokens is None
            or self.semi_stable_estimated_tokens is None
            or self.volatile_estimated_tokens is None
        ):
            return None
        return (
            self.stable_prefix_estimated_tokens
            + self.semi_stable_estimated_tokens
            + self.volatile_estimated_tokens
        )

    @property
    def total_bytes(self) -> int:
        """Sum of segment byte sizes across all kinds."""
        return self.stable_prefix_bytes + self.semi_stable_bytes + self.volatile_bytes

    def all_segments(self) -> tuple[RequestSegment, ...]:
        """All segments, in declaration order.  Alias for ``segments``."""
        return self.segments


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


def _ascii_chars(value: str) -> int:
    """Count ASCII characters in ``value`` (non-ASCII count as 0)."""
    return sum(1 for ch in value if ord(ch) < 128)


def _non_ascii_bytes(value: str) -> int:
    """Count UTF-8 byte length of non-ASCII characters in ``value``."""
    return sum(len(ch.encode("utf-8")) for ch in value if ord(ch) >= 128)


def _estimate_string_tokens(value: str) -> int:
    """Cheap token estimate for a single string value.

    Mirrors :func:`eggpool.request.limits._estimate_string_tokens`
    (4 ASCII chars/token, 2 non-ASCII bytes/token) but is duplicated
    here to keep this module self-contained for callers that need
    segment-level estimates without importing the full limits
    machinery.  Returns 0 for empty strings.
    """
    if not value:
        return 0
    ascii_chars = _ascii_chars(value)
    non_ascii_bytes = _non_ascii_bytes(value)
    ascii_tokens = (ascii_chars + 3) // 4
    non_ascii_tokens = (non_ascii_bytes + 1) // 2
    return ascii_tokens + non_ascii_tokens


def _estimate_value_tokens(value: Any) -> int:
    """Recursive token estimator for arbitrary JSON-compatible values.

    Strings use the cheap ASCII/non-ASCII estimator.  Mappings add a
    tiny per-key separator cost to approximate role labels; lists sum
    child estimates.  Numbers and booleans are tokenised via
    ``str(value)`` for consistency with the limits helper.  This
    function never raises on malformed input — circular references,
    broken ``__str__``, or unknown types fall back to a small
    positive estimate so callers can rely on a non-negative integer.
    """
    try:
        if isinstance(value, str):
            return _estimate_string_tokens(value)
        if isinstance(value, Mapping):
            mapping = cast("Mapping[Any, Any]", value)
            total = 1
            for k, v in mapping.items():
                total += _estimate_string_tokens(_safe_str(k))
                total += _estimate_value_tokens(v)
                total += 1
            return total
        if isinstance(value, list):
            items = cast("list[Any]", value)
            return sum(_estimate_value_tokens(item) for item in items)
        if value is None or isinstance(value, bool):
            return 1
        if isinstance(value, (int, float)):
            return max(1, (len(_safe_str(value)) + 3) // 4)
        return max(1, (len(_safe_str(value)) + 3) // 4)
    except Exception:  # noqa: BLE001
        return 1


def _safe_str(value: Any) -> str:
    """Return ``str(value)`` or a safe fallback if ``__str__`` raises."""
    try:
        return str(value)
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# OpenAI segmentation
# ---------------------------------------------------------------------------


# Marker patterns for volatile-suffix detection in tool outputs and the
# latest user turn.  These are intentionally conservative — false
# positives only enlarge the candidate set, while false negatives would
# under-classify a real log/script blob as semi-stable context.
_LOG_MARKER_PATTERNS: tuple[str, ...] = (
    "Traceback (most recent call last):",
    "stack trace",
    "FAILED ",
    "PASSED ",
    "ok ",
    "error:",
    "ERROR:",
    "Exception:",
    'File "',
    '.py", line ',
)
_COMMAND_OUTPUT_PATTERNS: tuple[str, ...] = (
    "$ ",
    "> ",
    "user@",
    "root@",
    "total ",
    "drwx",
    "-rw-",
)
_SEARCH_RESULT_PATTERNS: tuple[str, ...] = (
    "---",
    "diff --git",
    "@@ ",
    "Binary file",
    "Found ",
    "matches in",
    "grep:",
)


def _looks_like_command_output(text: str) -> bool:
    """True if ``text`` resembles terminal/command output."""
    return any(pattern in text for pattern in _COMMAND_OUTPUT_PATTERNS)


def _looks_like_search_result(text: str) -> bool:
    """True if ``text`` resembles grep/diff/file-search output."""
    return any(pattern in text for pattern in _SEARCH_RESULT_PATTERNS)


def _looks_like_log_output(text: str) -> bool:
    """True if ``text`` resembles test logs, stack traces, or compiler errors."""
    return any(pattern in text for pattern in _LOG_MARKER_PATTERNS)


def _classify_volatile_source(text: str) -> SegmentSource:
    """Best-effort classifier for volatile-suffix content.

    The classifier only returns a non-``UNKNOWN`` source when a marker
    pattern is found; otherwise the source stays ``UNKNOWN`` and the
    caller still records the region as ``volatile_suffix`` so the
    compressible-candidate flag is set.
    """
    if _looks_like_log_output(text):
        return SegmentSource.COMMAND_OUTPUT
    if _looks_like_command_output(text):
        return SegmentSource.COMMAND_OUTPUT
    if _looks_like_search_result(text):
        return SegmentSource.SEARCH_RESULT
    return SegmentSource.UNKNOWN


def _extract_text(value: Any) -> str:
    """Best-effort string extraction from an OpenAI message ``content``.

    ``content`` is either a string, a list of content parts, or ``None``.
    Each part may be a dict with a ``text`` field (or ``content`` for
    tool results) or a non-text block we ignore.
    """
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    items = cast("list[Any]", value)
    for part in items:
        if not isinstance(part, Mapping):
            continue
        part_map = cast("Mapping[str, Any]", part)
        part_type = part_map.get("type")
        if part_type in {"text", "input_text"}:
            text_value = part_map.get("text")
            if isinstance(text_value, str):
                parts.append(text_value)
        elif part_type == "image_url":
            continue
        else:
            # Tool-role content blocks carry their text under ``content``.
            text_value = part_map.get("content")
            if isinstance(text_value, str):
                parts.append(text_value)
    return "\n".join(parts)


def _serialize_for_hash(value: Any) -> str:
    """Stable JSON encoding for structural hashing.

    Uses ``sort_keys=True`` so semantically equivalent payloads
    (different key order) hash identically.  ``default=str`` mirrors
    the existing model_info pattern so non-JSON-native values do not
    raise.
    """
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


def _hash_payload(value: Any) -> str:
    """Return the hex SHA-256 digest of ``value`` as a stable string."""
    return hashlib.sha256(_serialize_for_hash(value).encode("utf-8")).hexdigest()


def _segment_openai_tools(tools: Any) -> list[RequestSegment]:
    """Classify the ``tools`` array of an OpenAI Chat Completions request.

    Every tool schema is a protected ``stable_prefix`` segment: tool
    descriptions are provider-cacheable, marked protected, and never
    compressible.
    """
    if not isinstance(tools, list):
        return []
    tool_list = cast("list[Any]", tools)
    segments: list[RequestSegment] = []
    for index, tool in enumerate(tool_list):
        byte_length = len(_serialize_for_hash(tool))
        estimated = _estimate_value_tokens(tool)
        segments.append(
            RequestSegment(
                kind=SegmentKind.STABLE_PREFIX,
                source=SegmentSource.TOOL_SCHEMA,
                message_index=None,
                content_path=("tools", index),
                byte_length=byte_length,
                estimated_tokens=estimated,
                protected=True,
                compressible_candidate=False,
                reason="tool_schema",
            )
        )
    return segments


def _segment_openai_message(
    role: str,
    content: Any,
    *,
    message_index: int,
    is_last: bool,
) -> RequestSegment:
    """Classify a single OpenAI ``messages`` entry.

    Rules (conservative — when uncertain we keep content as
    ``semi_stable_context``):

    * ``role == "system"`` or ``role == "developer"`` → stable_prefix
      and protected (the system prompt is provider-cacheable).
    * ``role == "tool"`` → volatile_suffix, source ``tool_result``.
      Tool outputs are exactly what later compression phases target.
    * ``role == "user"`` and ``is_last`` → volatile_suffix unless the
      content is short and free of log/command/search markers.
    * All other roles (assistant, prior user turns, prior tool
      messages) → semi_stable_context.
    """
    text = _extract_text(content)
    byte_length = len(_serialize_for_hash(content))
    estimated = (
        _estimate_string_tokens(text) if text else _estimate_value_tokens(content)
    )

    if role in {"system", "developer"}:
        return RequestSegment(
            kind=SegmentKind.STABLE_PREFIX,
            source=(
                SegmentSource.SYSTEM if role == "system" else SegmentSource.DEVELOPER
            ),
            message_index=message_index,
            content_path=(role,),
            byte_length=byte_length,
            estimated_tokens=estimated,
            protected=True,
            compressible_candidate=False,
            reason=f"role={role}",
        )

    if role == "tool":
        source = _classify_volatile_source(text)
        if source is SegmentSource.UNKNOWN:
            source = SegmentSource.TOOL_RESULT
        return RequestSegment(
            kind=SegmentKind.VOLATILE_SUFFIX,
            source=source,
            message_index=message_index,
            content_path=("messages", message_index, "tool"),
            byte_length=byte_length,
            estimated_tokens=estimated,
            protected=False,
            compressible_candidate=True,
            reason="tool_result",
        )

    if role == "user" and is_last:
        # Latest user turn is volatile by default unless it is very
        # short and shows no log/command/search markers — the latter
        # is the canonical "user typed a quick follow-up" case.
        if (
            text
            and len(text) <= 256
            and not _looks_like_log_output(text)
            and not _looks_like_command_output(text)
            and not _looks_like_search_result(text)
        ):
            return RequestSegment(
                kind=SegmentKind.SEMI_STABLE_CONTEXT,
                source=SegmentSource.LATEST_USER_MESSAGE,
                message_index=message_index,
                content_path=("messages", message_index, "user"),
                byte_length=byte_length,
                estimated_tokens=estimated,
                protected=False,
                compressible_candidate=False,
                reason="latest_user_short_instruction",
            )
        source = _classify_volatile_source(text)
        return RequestSegment(
            kind=SegmentKind.VOLATILE_SUFFIX,
            source=source,
            message_index=message_index,
            content_path=("messages", message_index, "user"),
            byte_length=byte_length,
            estimated_tokens=estimated,
            protected=False,
            compressible_candidate=True,
            reason="latest_user_message",
        )

    if role == "assistant":
        return RequestSegment(
            kind=SegmentKind.SEMI_STABLE_CONTEXT,
            source=SegmentSource.PRIOR_MESSAGE,
            message_index=message_index,
            content_path=("messages", message_index, "assistant"),
            byte_length=byte_length,
            estimated_tokens=estimated,
            protected=False,
            compressible_candidate=False,
            reason="assistant_message",
        )

    return RequestSegment(
        kind=SegmentKind.SEMI_STABLE_CONTEXT,
        source=SegmentSource.PRIOR_MESSAGE,
        message_index=message_index,
        content_path=("messages", message_index, role or "unknown"),
        byte_length=byte_length,
        estimated_tokens=estimated,
        protected=False,
        compressible_candidate=False,
        reason=f"ambiguous_role={role or 'unknown'}",
    )


def _segment_openai(payload: Mapping[str, Any]) -> list[RequestSegment]:
    """Segment an OpenAI Chat Completions payload.

    Stable prefix: top-level ``tools`` array plus any
    system/developer messages.  Semi-stable: prior assistant/user
    turns.  Volatile suffix: the latest user turn (when non-trivial)
    plus any ``role: "tool"`` messages.
    """
    segments: list[RequestSegment] = []
    tools = payload.get("tools")
    segments.extend(_segment_openai_tools(tools))

    messages = payload.get("messages")
    if not isinstance(messages, list):
        return segments
    message_list = cast("list[Any]", messages)
    last_index = len(message_list) - 1
    for index, raw_message in enumerate(message_list):
        if not isinstance(raw_message, Mapping):
            continue
        message_map = cast("Mapping[str, Any]", raw_message)
        role_value = message_map.get("role")
        role = role_value if isinstance(role_value, str) else ""
        content = message_map.get("content")
        segments.append(
            _segment_openai_message(
                role,
                content,
                message_index=index,
                is_last=(index == last_index),
            )
        )
    return segments


# ---------------------------------------------------------------------------
# Anthropic segmentation
# ---------------------------------------------------------------------------


def _extract_anthropic_text(content: Any) -> str:
    """Best-effort text extraction from Anthropic content blocks.

    Returns concatenated text from ``text`` blocks; non-text blocks
    (``image``, ``tool_use``, ``tool_result``) contribute their textual
    sub-fields when available.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    items = cast("list[Any]", content)
    for block in items:
        if not isinstance(block, Mapping):
            continue
        block_map = cast("Mapping[str, Any]", block)
        block_type = block_map.get("type")
        if block_type == "text":
            text_value = block_map.get("text")
            if isinstance(text_value, str):
                parts.append(text_value)
        elif block_type == "thinking":
            text_value = block_map.get("thinking")
            if isinstance(text_value, str):
                parts.append(text_value)
        elif block_type == "tool_use":
            input_value = block_map.get("input")
            if isinstance(input_value, str):
                parts.append(input_value)
            else:
                parts.append(_serialize_for_hash(input_value))
        elif block_type == "tool_result":
            inner_content = block_map.get("content")
            if isinstance(inner_content, str):
                parts.append(inner_content)
            else:
                parts.append(_serialize_for_hash(inner_content))
    return "\n".join(parts)


def _segment_anthropic_system(payload: Mapping[str, Any]) -> list[RequestSegment]:
    """Classify Anthropic's top-level ``system`` field.

    The top-level system is provider-cacheable; it is a protected
    stable-prefix region.  ``cache_control`` annotations on system
    blocks are flagged separately as protected metadata so the
    stable-prefix hash reflects cache-boundary decisions.
    """
    system = payload.get("system")
    if system is None:
        return []
    byte_length = len(_serialize_for_hash(system))
    estimated = _estimate_value_tokens(system)
    has_cache_control = False
    if isinstance(system, list):
        for block in cast("list[Any]", system):
            if isinstance(block, Mapping) and cast("Mapping[str, Any]", block).get(
                "cache_control"
            ):
                has_cache_control = True
                break
    segments: list[RequestSegment] = [
        RequestSegment(
            kind=SegmentKind.STABLE_PREFIX,
            source=SegmentSource.SYSTEM,
            message_index=None,
            content_path=("system",),
            byte_length=byte_length,
            estimated_tokens=estimated,
            protected=True,
            compressible_candidate=False,
            reason="top_level_system",
        )
    ]
    if has_cache_control:
        segments.append(
            RequestSegment(
                kind=SegmentKind.STABLE_PREFIX,
                source=SegmentSource.CACHE_CONTROL,
                message_index=None,
                content_path=("system", "cache_control"),
                byte_length=0,
                estimated_tokens=0,
                protected=True,
                compressible_candidate=False,
                reason="cache_control_present",
            )
        )
    return segments


def _segment_anthropic_tools(tools: Any) -> list[RequestSegment]:
    """Classify Anthropic's ``tools`` array."""
    if not isinstance(tools, list):
        return []
    tool_list = cast("list[Any]", tools)
    segments: list[RequestSegment] = []
    for index, tool in enumerate(tool_list):
        if not isinstance(tool, Mapping):
            continue
        tool_map = cast("Mapping[str, Any]", tool)
        byte_length = len(_serialize_for_hash(tool))
        estimated = _estimate_value_tokens(tool)
        segments.append(
            RequestSegment(
                kind=SegmentKind.STABLE_PREFIX,
                source=SegmentSource.TOOL_SCHEMA,
                message_index=None,
                content_path=("tools", index),
                byte_length=byte_length,
                estimated_tokens=estimated,
                protected=True,
                compressible_candidate=False,
                reason="tool_schema",
            )
        )
        if tool_map.get("cache_control"):
            segments.append(
                RequestSegment(
                    kind=SegmentKind.STABLE_PREFIX,
                    source=SegmentSource.CACHE_CONTROL,
                    message_index=None,
                    content_path=("tools", index, "cache_control"),
                    byte_length=0,
                    estimated_tokens=0,
                    protected=True,
                    compressible_candidate=False,
                    reason="cache_control_present",
                )
            )
    return segments


def _segment_anthropic_message_block(
    block: Any,
    *,
    message_index: int,
    block_index: int,
) -> RequestSegment:
    """Classify a single Anthropic content block within a message."""
    if not isinstance(block, Mapping):
        return RequestSegment(
            kind=SegmentKind.SEMI_STABLE_CONTEXT,
            source=SegmentSource.PRIOR_MESSAGE,
            message_index=message_index,
            content_path=("messages", message_index, block_index, "unknown"),
            byte_length=len(_serialize_for_hash(block)),
            estimated_tokens=_estimate_value_tokens(block),
            protected=False,
            compressible_candidate=False,
            reason="unknown_block",
        )
    block_map = cast("Mapping[str, Any]", block)
    block_type = block_map.get("type")
    byte_length = len(_serialize_for_hash(block))
    estimated = _estimate_value_tokens(block)
    text = _extract_anthropic_text([block])

    if block_type == "text" and block_map.get("cache_control"):
        return RequestSegment(
            kind=SegmentKind.STABLE_PREFIX,
            source=SegmentSource.CACHE_CONTROL,
            message_index=message_index,
            content_path=("messages", message_index, block_index, "text"),
            byte_length=byte_length,
            estimated_tokens=estimated,
            protected=True,
            compressible_candidate=False,
            reason="cache_control_text",
        )
    if block_type == "thinking":
        return RequestSegment(
            kind=SegmentKind.STABLE_PREFIX,
            source=SegmentSource.CACHE_CONTROL,
            message_index=message_index,
            content_path=("messages", message_index, block_index, "thinking"),
            byte_length=byte_length,
            estimated_tokens=estimated,
            protected=True,
            compressible_candidate=False,
            reason="thinking_block",
        )
    if block_type == "tool_use":
        return RequestSegment(
            kind=SegmentKind.SEMI_STABLE_CONTEXT,
            source=SegmentSource.PRIOR_MESSAGE,
            message_index=message_index,
            content_path=("messages", message_index, block_index, "tool_use"),
            byte_length=byte_length,
            estimated_tokens=estimated,
            protected=False,
            compressible_candidate=False,
            reason="tool_use_history",
        )
    if block_type == "tool_result":
        source = _classify_volatile_source(text)
        if source is SegmentSource.UNKNOWN:
            source = SegmentSource.TOOL_RESULT
        return RequestSegment(
            kind=SegmentKind.VOLATILE_SUFFIX,
            source=source,
            message_index=message_index,
            content_path=("messages", message_index, block_index, "tool_result"),
            byte_length=byte_length,
            estimated_tokens=estimated,
            protected=False,
            compressible_candidate=True,
            reason="tool_result",
        )
    if block_type == "text":
        return RequestSegment(
            kind=SegmentKind.SEMI_STABLE_CONTEXT,
            source=SegmentSource.PRIOR_MESSAGE,
            message_index=message_index,
            content_path=("messages", message_index, block_index, "text"),
            byte_length=byte_length,
            estimated_tokens=estimated,
            protected=False,
            compressible_candidate=False,
            reason="text_block",
        )
    if block_type in {"image", "document"}:
        return RequestSegment(
            kind=SegmentKind.SEMI_STABLE_CONTEXT,
            source=SegmentSource.PRIOR_MESSAGE,
            message_index=message_index,
            content_path=("messages", message_index, block_index, str(block_type)),
            byte_length=byte_length,
            estimated_tokens=estimated,
            protected=False,
            compressible_candidate=False,
            reason="binary_block",
        )
    return RequestSegment(
        kind=SegmentKind.SEMI_STABLE_CONTEXT,
        source=SegmentSource.PRIOR_MESSAGE,
        message_index=message_index,
        content_path=("messages", message_index, block_index, "unknown"),
        byte_length=byte_length,
        estimated_tokens=estimated,
        protected=False,
        compressible_candidate=False,
        reason="unrecognised_block",
    )


def _segment_anthropic_message(
    message: Any,
    *,
    message_index: int,
    is_last: bool,
) -> list[RequestSegment]:
    """Classify a single Anthropic ``messages`` entry.

    Returns one segment per content block.  The final user turn is
    scanned for tool_result / log content; if found, the volatile
    suffix grows accordingly.  Non-text content (images, documents)
    is treated as semi-stable context — the segmentation layer is
    observational and never marks them compressible by default.
    """
    if not isinstance(message, Mapping):
        return []
    message_map = cast("Mapping[str, Any]", message)
    role_value = message_map.get("role")
    role = role_value if isinstance(role_value, str) else ""
    content = message_map.get("content")
    if isinstance(content, str):
        if role == "user" and is_last:
            text = content
            if (
                text
                and len(text) <= 256
                and not _looks_like_log_output(text)
                and not _looks_like_command_output(text)
                and not _looks_like_search_result(text)
            ):
                return [
                    RequestSegment(
                        kind=SegmentKind.SEMI_STABLE_CONTEXT,
                        source=SegmentSource.LATEST_USER_MESSAGE,
                        message_index=message_index,
                        content_path=("messages", message_index, "user"),
                        byte_length=len(_serialize_for_hash(content)),
                        estimated_tokens=_estimate_string_tokens(text),
                        protected=False,
                        compressible_candidate=False,
                        reason="latest_user_short_instruction",
                    )
                ]
            source = _classify_volatile_source(text)
            return [
                RequestSegment(
                    kind=SegmentKind.VOLATILE_SUFFIX,
                    source=source,
                    message_index=message_index,
                    content_path=("messages", message_index, "user"),
                    byte_length=len(_serialize_for_hash(content)),
                    estimated_tokens=_estimate_string_tokens(text),
                    protected=False,
                    compressible_candidate=True,
                    reason="latest_user_message",
                )
            ]
        # Assistant text or non-final user text → semi-stable.
        return [
            RequestSegment(
                kind=SegmentKind.SEMI_STABLE_CONTEXT,
                source=SegmentSource.PRIOR_MESSAGE,
                message_index=message_index,
                content_path=("messages", message_index, role or "user"),
                byte_length=len(_serialize_for_hash(content)),
                estimated_tokens=_estimate_string_tokens(content),
                protected=False,
                compressible_candidate=False,
                reason=f"role={role or 'user'}",
            )
        ]
    if not isinstance(content, list):
        return []
    block_list = cast("list[Any]", content)
    return [
        _segment_anthropic_message_block(
            block,
            message_index=message_index,
            block_index=index,
        )
        for index, block in enumerate(block_list)
    ]


def _segment_anthropic(payload: Mapping[str, Any]) -> list[RequestSegment]:
    """Segment an Anthropic Messages payload."""
    segments: list[RequestSegment] = []
    segments.extend(_segment_anthropic_system(payload))
    segments.extend(_segment_anthropic_tools(payload.get("tools")))

    messages = payload.get("messages")
    if not isinstance(messages, list):
        return segments
    message_list = cast("list[Any]", messages)
    last_index = len(message_list) - 1
    for index, raw_message in enumerate(message_list):
        segments.extend(
            _segment_anthropic_message(
                raw_message,
                message_index=index,
                is_last=(index == last_index),
            )
        )
    return segments


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _empty_request_result() -> SegmentationResult:
    """Return a stable empty result for requests with no segmentable content."""
    counts: dict[SegmentKind, int] = {
        SegmentKind.STABLE_PREFIX: 0,
        SegmentKind.SEMI_STABLE_CONTEXT: 0,
        SegmentKind.VOLATILE_SUFFIX: 0,
    }
    return SegmentationResult(
        status=SegmentationStatus.EMPTY_REQUEST,
        segments=(),
        segment_count_by_kind=counts,
        stable_prefix_bytes=0,
        semi_stable_bytes=0,
        volatile_bytes=0,
        stable_prefix_estimated_tokens=0,
        semi_stable_estimated_tokens=0,
        volatile_estimated_tokens=0,
        stable_prefix_hash=_hash_payload({}),
        request_shape_hash=_hash_payload({}),
        cache_control_present=False,
    )


def _parse_failure_result() -> SegmentationResult:
    """Return a stable result for unparseable payloads."""
    counts: dict[SegmentKind, int] = {
        SegmentKind.STABLE_PREFIX: 0,
        SegmentKind.SEMI_STABLE_CONTEXT: 0,
        SegmentKind.VOLATILE_SUFFIX: 0,
    }
    return SegmentationResult(
        status=SegmentationStatus.PARSE_FAILURE,
        segments=(),
        segment_count_by_kind=counts,
        stable_prefix_bytes=0,
        semi_stable_bytes=0,
        volatile_bytes=0,
        stable_prefix_estimated_tokens=0,
        semi_stable_estimated_tokens=0,
        volatile_estimated_tokens=0,
        stable_prefix_hash="",
        request_shape_hash="",
        cache_control_present=False,
    )


def _shape_descriptor(
    payload: Mapping[str, Any],
    *,
    protocol: str,
    segments: tuple[RequestSegment, ...],
) -> dict[str, Any]:
    """Build a content-private shape descriptor for ``request_shape_hash``.

    The descriptor captures only structural information (provider,
    model, role sequence, content-block sequence, tool schema count,
    cache-control presence, volatile-suffix source) so the resulting
    hash is stable across payloads that differ only in their actual
    text but are otherwise structurally identical.
    """
    model_value = payload.get("model")
    messages = payload.get("messages")
    role_sequence: list[str] = []
    block_type_sequence: list[str] = []
    if isinstance(messages, list):
        for message in cast("list[Any]", messages):
            if not isinstance(message, Mapping):
                role_sequence.append("?")
                block_type_sequence.append("?")
                continue
            message_map = cast("Mapping[str, Any]", message)
            role_value = message_map.get("role")
            role_sequence.append(role_value if isinstance(role_value, str) else "?")
            content = message_map.get("content")
            if isinstance(content, list):
                for block in cast("list[Any]", content):
                    if isinstance(block, Mapping):
                        block_map = cast("Mapping[str, Any]", block)
                        block_type = block_map.get("type")
                        block_type_sequence.append(
                            block_type if isinstance(block_type, str) else "?"
                        )
                    else:
                        block_type_sequence.append("?")
            else:
                block_type_sequence.append("text" if isinstance(content, str) else "?")

    tools = payload.get("tools")
    tool_schema_count = len(cast("list[Any]", tools)) if isinstance(tools, list) else 0
    cache_control_present = any(
        s.source is SegmentSource.CACHE_CONTROL for s in segments
    )
    volatile_source_set = sorted(
        {s.source.value for s in segments if s.kind is SegmentKind.VOLATILE_SUFFIX}
    )
    stable = sum(
        s.estimated_tokens or 0 for s in segments if s.kind is SegmentKind.STABLE_PREFIX
    )
    semi = sum(
        s.estimated_tokens or 0
        for s in segments
        if s.kind is SegmentKind.SEMI_STABLE_CONTEXT
    )
    volatile = sum(
        s.estimated_tokens or 0
        for s in segments
        if s.kind is SegmentKind.VOLATILE_SUFFIX
    )
    token_buckets = {
        "stable_prefix_bucket": _bucketize(stable),
        "semi_stable_bucket": _bucketize(semi),
        "volatile_bucket": _bucketize(volatile),
    }
    return {
        "protocol": protocol,
        "model": model_value if isinstance(model_value, str) else "",
        "role_sequence": role_sequence,
        "block_type_sequence": block_type_sequence,
        "tool_schema_count": tool_schema_count,
        "cache_control_present": cache_control_present,
        "volatile_sources": volatile_source_set,
        "token_buckets": token_buckets,
        "segment_count_by_kind": {
            k.value: v for k, v in _count_by_kind(segments).items()
        },
    }


def _bucketize(tokens: int) -> str:
    """Bucket token counts into coarse ranges for content-private hashing.

    Buckets keep the request-shape hash stable across tiny textual
    differences while still letting the dashboard distinguish "tiny"
    from "huge" requests.  Boundaries are powers of 4 around the
    typical prompt size range.
    """
    if tokens <= 0:
        return "0"
    if tokens <= 256:
        return "0-256"
    if tokens <= 1_024:
        return "256-1k"
    if tokens <= 4_096:
        return "1k-4k"
    if tokens <= 16_384:
        return "4k-16k"
    if tokens <= 65_536:
        return "16k-65k"
    if tokens <= 262_144:
        return "65k-262k"
    return "262k+"


def _count_by_kind(
    segments: tuple[RequestSegment, ...],
) -> dict[SegmentKind, int]:
    """Tally segments by kind, always returning all three buckets."""
    counts: dict[SegmentKind, int] = {
        SegmentKind.STABLE_PREFIX: 0,
        SegmentKind.SEMI_STABLE_CONTEXT: 0,
        SegmentKind.VOLATILE_SUFFIX: 0,
    }
    for segment in segments:
        counts[segment.kind] += 1
    return counts


def _stable_prefix_descriptor(
    segments: tuple[RequestSegment, ...],
) -> dict[str, Any]:
    """Build a content-private descriptor of the stable prefix.

    The descriptor captures only structural information (sources,
    byte sizes, message indices, content paths) so the resulting
    stable_prefix_hash is identical for structurally-equivalent
    stable prefixes — even when the actual prompt text differs in
    insignificant ways (whitespace, minor wording).
    """
    stable_segments = [s for s in segments if s.kind is SegmentKind.STABLE_PREFIX]
    return {
        "segment_count": len(stable_segments),
        "byte_total": sum(s.byte_length for s in stable_segments),
        "token_total": sum(s.estimated_tokens or 0 for s in stable_segments),
        "sources": sorted({s.source.value for s in stable_segments}),
        "path_signatures": [
            {
                "source": s.source.value,
                "path": [str(p) for p in s.content_path],
                "message_index": s.message_index,
            }
            for s in stable_segments
        ],
    }


def _build_result(
    payload: Mapping[str, Any],
    *,
    protocol: str,
    segments: list[RequestSegment],
) -> SegmentationResult:
    """Aggregate ``segments`` into a :class:`SegmentationResult`."""
    segment_tuple = tuple(segments)
    counts = _count_by_kind(segment_tuple)
    stable_bytes = sum(
        s.byte_length for s in segment_tuple if s.kind is SegmentKind.STABLE_PREFIX
    )
    semi_bytes = sum(
        s.byte_length
        for s in segment_tuple
        if s.kind is SegmentKind.SEMI_STABLE_CONTEXT
    )
    volatile_bytes = sum(
        s.byte_length for s in segment_tuple if s.kind is SegmentKind.VOLATILE_SUFFIX
    )
    stable_tokens = sum(
        s.estimated_tokens or 0
        for s in segment_tuple
        if s.kind is SegmentKind.STABLE_PREFIX
    )
    semi_tokens = sum(
        s.estimated_tokens or 0
        for s in segment_tuple
        if s.kind is SegmentKind.SEMI_STABLE_CONTEXT
    )
    volatile_tokens = sum(
        s.estimated_tokens or 0
        for s in segment_tuple
        if s.kind is SegmentKind.VOLATILE_SUFFIX
    )
    status = (
        SegmentationStatus.SEGMENTED
        if segment_tuple
        else SegmentationStatus.EMPTY_REQUEST
    )
    shape = _shape_descriptor(payload, protocol=protocol, segments=segment_tuple)
    shape_hash = _hash_payload(shape)
    stable_prefix_hash = _hash_payload(_stable_prefix_descriptor(segment_tuple))
    cache_control_present = any(
        s.source is SegmentSource.CACHE_CONTROL for s in segment_tuple
    )
    return SegmentationResult(
        status=status,
        segments=segment_tuple,
        segment_count_by_kind=counts,
        stable_prefix_bytes=stable_bytes,
        semi_stable_bytes=semi_bytes,
        volatile_bytes=volatile_bytes,
        stable_prefix_estimated_tokens=stable_tokens,
        semi_stable_estimated_tokens=semi_tokens,
        volatile_estimated_tokens=volatile_tokens,
        stable_prefix_hash=stable_prefix_hash,
        request_shape_hash=shape_hash,
        cache_control_present=cache_control_present,
    )


def segment_request(
    payload: Any,
    *,
    protocol: str,
) -> SegmentationResult:
    """Segment a decoded request payload into cache/compression regions.

    Parameters
    ----------
    payload:
        Decoded JSON-compatible request body.  Any non-mapping value
        yields a :attr:`SegmentationStatus.PARSE_FAILURE` result; the
        function never raises on malformed input.
    protocol:
        ``"openai"`` or ``"anthropic"`` — selects which segmenter runs.
        Any other value yields a parse-failure result so the caller
        always receives a stable :class:`SegmentationResult` shape.

    Returns
    -------
    SegmentationResult
        Frozen summary of the segmentation pass.  Never raises.
    """
    if not isinstance(payload, Mapping):
        return _parse_failure_result()
    payload_map = cast("Mapping[str, Any]", payload)
    if protocol == "openai":
        segments = _segment_openai(payload_map)
    elif protocol == "anthropic":
        segments = _segment_anthropic(payload_map)
    else:
        return _parse_failure_result()
    if not segments:
        return _empty_request_result()
    return _build_result(payload_map, protocol=protocol, segments=segments)


def segmentation_summary_json(result: SegmentationResult) -> str:
    """Serialise the segmentation summary for persistence.

    The full :class:`SegmentationResult` (segments + hashes) is encoded
    to a compact JSON string for storage in the
    ``segmentation_summary_json`` column.  Raw request content is
    never included.
    """
    payload = {
        "status": result.status.value,
        "segment_count_by_kind": {
            k.value: v for k, v in result.segment_count_by_kind.items()
        },
        "stable_prefix_bytes": result.stable_prefix_bytes,
        "semi_stable_bytes": result.semi_stable_bytes,
        "volatile_bytes": result.volatile_bytes,
        "stable_prefix_estimated_tokens": result.stable_prefix_estimated_tokens,
        "semi_stable_estimated_tokens": result.semi_stable_estimated_tokens,
        "volatile_estimated_tokens": result.volatile_estimated_tokens,
        "stable_prefix_hash": result.stable_prefix_hash,
        "request_shape_hash": result.request_shape_hash,
        "compressible_candidate_count": result.compressible_candidate_count(),
        "protected_count": result.protected_count(),
        "segments": [
            {
                "kind": s.kind.value,
                "source": s.source.value,
                "message_index": s.message_index,
                "content_path": [str(p) for p in s.content_path],
                "byte_length": s.byte_length,
                "estimated_tokens": s.estimated_tokens,
                "protected": s.protected,
                "compressible_candidate": s.compressible_candidate,
                "reason": s.reason,
            }
            for s in result.segments
        ],
    }
    return json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)


__all__ = [
    "RequestSegment",
    "SegmentKind",
    "SegmentSource",
    "SegmentationResult",
    "SegmentationStatus",
    "segment_request",
    "segmentation_summary_json",
]
